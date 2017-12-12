
from datetime import datetime
from six import text_type
from collections import deque
from functools import reduce

from binascii import hexlify
from os import urandom
from uuid import uuid4

from zope.interface import implementer, implementedBy

from attr import Factory
from attr.validators import instance_of as an

import attr
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Boolean, Unicode,
    ForeignKey, DateTime, UniqueConstraint
)
from sqlalchemy.schema import CreateTable
from sqlalchemy.exc import OperationalError, IntegrityError
from alchimia import TWISTED_STRATEGY

from ..interfaces import (
    ISession, ISessionStore, NoSuchSession, ISimpleAccount,
    ISimpleAccountBinding, ISessionProcurer, ISQLSchemaComponent,
    TransactionEnded, ISQLAuthorizer
)

from .. import SessionProcurer

from twisted.internet.defer import (
    inlineCallbacks, returnValue, gatherResults, maybeDeferred
)
from twisted.python.compat import unicode
from twisted.python.failure import Failure

from .security import computeKeyText, checkAndReset

@implementer(ISession)
@attr.s
class SQLSession(object):
    _sessionStore = attr.ib()
    identifier = attr.ib()
    isConfidential = attr.ib()
    authenticatedBy = attr.ib()

    def authorize(self, interfaces):
        interfaces = set(interfaces)
        datastore = self._sessionStore._datastore
        @datastore.sql
        def authzn(txn):
            result = {}
            ds = []
            authorizers = datastore.componentsProviding(ISQLAuthorizer)
            for a in authorizers:
                # This should probably do something smart with interface
                # priority, checking isOrExtends or something similar.
                if a.authzn_for in interfaces:
                    v = maybeDeferred(a.authzn_for_session,
                                      self._sessionStore, txn, self)
                    ds.append(v)
                    result[a.authzn_for] = v
                    v.addCallback(
                        lambda value, ai: result.__setitem__(ai, value),
                        ai=a.authzn_for
                    )
            def r(ignored):
                return result
            return (gatherResults(ds).addCallback(r))
        return authzn



@attr.s
class SessionIPInformation(object):
    """
    Information about a session being used from a given IP address.
    """
    id = attr.ib(validator=an(text_type))
    ip = attr.ib(validator=an(text_type))
    when = attr.ib(validator=an(datetime))



@attr.s
class Transaction(object):
    """
    Wrapper around a SQLAlchemy connection which is invalidated when the
    transaction is committed or rolled back.
    """
    _connection = attr.ib()
    _stopped = attr.ib(default=False)

    def execute(self, statement, *multiparams, **params):
        """
        Execute a statement unless this transaction has been stopped, otherwise
        raise L{TransactionEnded}.
        """
        if self._stopped:
            raise TransactionEnded(self._stopped)
        return self._connection.execute(statement, *multiparams, **params)



@attr.s
class AlchimiaDataStore(object):
    """
    L{AlchimiaDataStore} is a generic storage object that connect to an SQL
    database, run transactions, and manage schema metadata.
    """

    _engine = attr.ib()
    _components = attr.ib()
    _free_connections = attr.ib(default=Factory(deque))

    @inlineCallbacks
    def sql(self, callable):
        """
        Run the given C{callable}.

        @param callable: A callable object that encapsulates application logic
            that needs to run in a transaction.
        @type callable: callable taking a L{Transaction} and returning a
            L{Deferred}.

        @return: a L{Deferred} firing with the result of C{callable}
        @rtype: L{Deferred} that fires when the transaction is complete, or
            fails when the transaction is rolled back.
        """
        try:
            cxn = (self._free_connections.popleft() if self._free_connections
                   else (yield self._engine.connect()))
            sqla_txn = yield cxn.begin()
            txn = Transaction(cxn)
            try:
                result = yield callable(txn)
            except:
                # XXX rollback() and commit() might both also fail
                failure = Failure()
                txn._stopped = "rolled back"
                yield sqla_txn.rollback()
                returnValue(failure)
            else:
                txn._stopped = "committed"
                yield sqla_txn.commit()
                returnValue(result)
        finally:
            self._free_connections.append(cxn)


    def componentsProviding(self, interface):
        """
        Get all the components providing the given interface.
        """
        for component in self._components:
            if interface.providedBy(component):
                # Adaptation-by-call doesn't work with implementedBy() objects
                # so we can't query for classes
                yield component


    @classmethod
    def open(cls, reactor, db_url, component_creators):
        """
        Open an L{AlchimiaDataStore}.

        @param reactor: the reactor that this store should be opened on.
        @type reactor: L{IReactorThreads}

        @param db_url: the SQLAlchemy database URI to connect to.
        @type db_url: L{str}

        @param component_creators: callables which can create components.
        @type component_creators: C{iterable} of L{callable} taking 2
            arguments; (L{MetaData}, L{AlchimiaDataStore}), and returning a
            non-C{None} value.
        """
        metadata = MetaData()
        components = []
        self = cls(
            create_engine(db_url, reactor=reactor, strategy=TWISTED_STRATEGY),
            components,
        )
        components.extend(creator(metadata, self)
                          for creator in component_creators)
        return self


    def _populate_schema(self):
        """
        Populate the schema.
        """
        @self.sql
        @inlineCallbacks
        def do(transaction):
            for component in self.componentsProviding(ISQLSchemaComponent):
                try:
                    yield component.initialize_schema(transaction)
                except OperationalError as oe:
                    # Table creation failure.  TODO: log this error.
                    print("OE:", oe)
        return do


@implementer(ISessionStore, ISQLSchemaComponent)
@attr.s(init=False)
class AlchimiaSessionStore(object):
    """
    
    """

    def __init__(self, metadata, datastore):
        """
        Create an L{AlchimiaSessionStore} from a L{AlchimiaStore}.
        """
        self._datastore = datastore
        self.session_table = Table(
            "session", metadata,
            Column("sessionID", Unicode(), primary_key=True, nullable=False),
            Column("confidential", Boolean(), nullable=False),
        )

    def sent_insecurely(self, tokens):
        """
        Tokens have been sent insecurely; delete any tokens expected to be
        confidential.

        @param tokens: L{list} of L{str}

        @return: a L{Deferred} that fires when the tokens have been
            invalidated.
        """
        @self._datastore.sql
        def invalidate(txn):
            s = self.session_table
            return gatherResults([
                txn.execute(
                    s.delete().where((s.c.sessionID == token) &
                                     (s.c.confidential == True))
                ) for token in tokens
            ])
        return invalidate


    @inlineCallbacks
    def initialize_schema(self, transaction):
        """
        Initialize session-specific schema.
        """
        try:
            yield transaction.execute(CreateTable(self.session_table))
        except OperationalError as oe:
            print("sessions-table", oe)


    def newSession(self, isConfidential, authenticatedBy):
        @self._datastore.sql
        @inlineCallbacks
        def created(txn):
            identifier = hexlify(urandom(32)).decode('ascii')
            s = self.session_table
            yield txn.execute(s.insert().values(
                sessionID=identifier,
                confidential=isConfidential,
            ))
            returnValue(SQLSession(self,
                                   identifier=identifier,
                                   isConfidential=isConfidential,
                                   authenticatedBy=authenticatedBy))
        return created


    def loadSession(self, identifier, isConfidential, authenticatedBy):
        @self._datastore.sql
        @inlineCallbacks
        def loaded(engine):
            s = self.session_table
            result = yield engine.execute(
                s.select((s.c.sessionID==identifier) &
                         (s.c.confidential==isConfidential)))
            results = yield result.fetchall()
            if not results:
                raise NoSuchSession()
            fetched_identifier = results[0][s.c.sessionID]
            returnValue(SQLSession(self,
                                   identifier=fetched_identifier,
                                   isConfidential=isConfidential,
                                   authenticatedBy=authenticatedBy))
        return loaded



@implementer(ISimpleAccountBinding)
@attr.s
class AccountSessionBinding(object):
    """
    (Stateless) binding between an account and a session, so that sessions can
    attach to, detach from, .
    """
    _plugin = attr.ib()
    _session = attr.ib()
    _datastore = attr.ib()

    def _account(self, accountID, username, email):
        """
        
        """
        return SQLAccount(self._plugin, self._datastore, accountID, username,
                          email)


    @inlineCallbacks
    def create_account(self, username, email, password):
        """
        Create a new account with the given username, email and password.

        @return: an L{Account} if one could be created, L{None} if one could
            not be.
        """
        computedHash = yield computeKeyText(password)
        @self._datastore.sql
        @inlineCallbacks
        def store(engine):
            newAccountID = unicode(uuid4())
            insert = (self._plugin.accountTable.insert()
                      .values(accountID=newAccountID,
                              username=username, email=email,
                              passwordBlob=computedHash))
            try:
                yield engine.execute(insert)
            except IntegrityError:
                returnValue(None)
            else:
                returnValue(newAccountID)
        accountID = (yield store)
        if accountID is None:
            returnValue(None)
        account = self._account(accountID, username, email)
        returnValue(account)


    @inlineCallbacks
    def log_in(self, username, password):
        """
        Associate this session with a given user account, if the password
        matches.

        @param username: The username input by the user.
        @type username: L{text_type}

        @param password: The plain-text password input by the user.
        @type password: L{text_type}

        @rtype: L{Deferred} firing with L{IAccount} if we succeeded and L{None}
            if we failed.
        """
        acc = self._plugin.accountTable
        @self._datastore.sql
        @inlineCallbacks
        def retrieve(engine):
            result = yield engine.execute(
                acc.select(acc.c.username == username)
            )
            returnValue((yield result.fetchall()))
        accountsInfo = yield retrieve
        if not accountsInfo:
            # no account, bye
            returnValue(None)
        [row] = accountsInfo
        stored_password_text = row[acc.c.passwordBlob]
        accountID = row[acc.c.accountID]

        def reset_password(newPWText):
            @self._datastore.sql
            def storenew(engine):
                a = self._plugin.accountTable
                return engine.execute(
                    a.update(a.c.accountID == accountID)
                    .values(passwordBlob=newPWText)
                )
            return storenew

        if (yield checkAndReset(stored_password_text,
                                  password,
                                  reset_password)):
            account = self._account(accountID, row[acc.c.username],
                                    row[acc.c.email])
            yield account.add_session(self._session)
            returnValue(account)


    def authenticated_accounts(self):
        """
        Retrieve the accounts currently associated with this session.

        @return: L{Deferred} firing with a L{list} of accounts.
        """
        @self._datastore.sql
        @inlineCallbacks
        def retrieve(engine):
            ast = self._plugin.account_session_table
            acc = self._plugin.accountTable
            result = (yield (yield engine.execute(
                ast.join(acc, ast.c.accountID == acc.c.accountID)
                .select(ast.c.sessionID == self._session.identifier,
                        use_labels=True)
            )).fetchall())
            returnValue([
                self._account(it[ast.c.accountID], it[acc.c.username],
                              it[acc.c.email])
                for it in result
            ])
        return retrieve


    def attached_sessions(self):
        """
        Retrieve information about all sessions attached to the same account
        that this session is.

        @return: L{Deferred} firing a L{list} of L{SessionIPInformation}
        """
        acs = self._plugin.account_session_table
        # XXX FIXME this is a bad way to access the table, since the table
        # is not actually part of the interface passed here
        sipt = (next(self._datastore.componentsProviding(
            implementedBy(IPTrackingProcurer)
        ))._session_ip_table)
        @self._datastore.sql
        @inlineCallbacks
        def query(conn):
            acs2 = acs.alias()
            from sqlalchemy.sql.expression import select
            result = yield conn.execute(
                select([sipt], use_labels=True)
                .where((acs.c.sessionID == self._session.identifier) &
                       (acs.c.accountID == acs2.c.accountID) &
                       (acs2.c.sessionID == sipt.c.sessionID)
                )
            )
            returnValue([
                SessionIPInformation(
                    id=row[sipt.c.sessionID],
                    ip=row[sipt.c.ip_address],
                    when=row[sipt.c.last_used])
                for row in (yield result.fetchall())
            ])
        return query


    def log_out(self):
        """
        Disassociate this session from any accounts it's logged in to.

        @return: a L{Deferred} that fires when the account is logged out.
        """
        @self._datastore.sql
        def retrieve(engine):
            ast = self._plugin.account_session_table
            return engine.execute(ast.delete(
                ast.c.sessionID == self._session.identifier
            ))
        return retrieve




@implementer(ISimpleAccount)
@attr.s
class SQLAccount(object):
    """
    
    """
    _plugin = attr.ib()
    _datastore = attr.ib()
    accountID = attr.ib()
    username = attr.ib()
    email = attr.ib()

    def add_session(self, session):
        """
        
        """
        @self._datastore.sql
        def createrow(engine):
            return engine.execute(
                self._plugin.account_session_table
                .insert().values(accountID=self.accountID,
                                 sessionID=session.identifier)
            )
        return createrow


    @inlineCallbacks
    def change_password(self, new_password):
        """
        @param new_password: The text of the new password.
        @type new_password: L{unicode}
        """
        computed_hash = computeKeyText(new_password)
        @self._datastore.sql
        def change(engine):
            return engine.execute(
                self._plugin.accountTable.update()
                .where(accountID=self.accountID)
                .values(passwordBlob=computed_hash)
            )
        returnValue((yield change))



@implementer(ISQLAuthorizer, ISQLSchemaComponent)
class AccountBindingStorePlugin(object):
    """
    
    """

    authzn_for = ISimpleAccountBinding

    def __init__(self, metadata, store):
        """
        
        """
        self._datastore = store

        self.accountTable = Table(
            "account", metadata,
            Column("accountID", Unicode(), primary_key=True,
                   nullable=False),
            Column("username", Unicode(), unique=True, nullable=False),
            Column("email", Unicode(), nullable=False),
            Column("passwordBlob", Unicode(), nullable=False),
        )

        self.account_session_table = Table(
            "account_session", metadata,
            Column("accountID", Unicode(),
                   ForeignKey("account.accountID", ondelete="CASCADE")),
            Column("sessionID", Unicode(),
                   ForeignKey("session.sessionID", ondelete="CASCADE")),
            UniqueConstraint("accountID", "sessionID"),
        )

    @inlineCallbacks
    def initialize_schema(self, transaction):
        """
        
        """
        for table in [self.accountTable, self.account_session_table]:
            yield transaction.execute(CreateTable(table))


    def authzn_for_session(self, session_store, transaction, session):
        return AccountSessionBinding(self, session, self._datastore)


@implementer(ISQLAuthorizer)
class AccountLoginAuthorizer(object):
    """
    
    """

    authzn_for = ISimpleAccount

    def __init__(self, metadata, store):
        """
        
        """
        self.datastore = store

    @inlineCallbacks
    def authzn_for_session(self, session_store, transaction, session):
        """
        
        """
        binding = (yield session.authorize([ISimpleAccountBinding])
                   )[ISimpleAccountBinding]
        returnValue(next(iter((yield binding.authenticated_accounts())),
                         None))



@inlineCallbacks
def upsert(engine, table, to_query, to_change):
    """
    Try inserting, if inserting fails, then update.
    """
    try:
        result = yield engine.execute(
            table.insert().values(**dict(to_query, **to_change))
        )
    except IntegrityError:
        from operator import and_ as And
        update = table.update().where(
            reduce(And, (
                (getattr(table.c, cname) == cvalue)
                for (cname, cvalue) in to_query.items()
            ))
        ).values(**to_change)
        result = yield engine.execute(update)
    returnValue(result)



@implementer(ISessionProcurer, ISQLSchemaComponent)
class IPTrackingProcurer(object):

    def __init__(self, metadata, datastore, procurer):
        """
        
        """
        self._session_ip_table = Table(
            "session_ip", metadata,
            Column("sessionID", Unicode(),
                   ForeignKey("session.sessionID", ondelete="CASCADE"),
                   nullable=False),
            Column("ip_address", Unicode(), nullable=False),
            Column("address_family", Unicode(), nullable=False),
            Column("last_used", DateTime(), nullable=False),
            UniqueConstraint("sessionID", "ip_address", "address_family"),
        )
        self._datastore = datastore
        self._procurer = procurer


    @inlineCallbacks
    def initialize_schema(self, transaction):
        """
        
        """
        try:
            yield transaction.execute(CreateTable(self._session_ip_table))
        except OperationalError as oe:
            print("ip-schema", oe)


    def procureSession(self, request, forceInsecure=False,
                        alwaysCreate=True):
        andThen = (self._procurer
                   .procureSession(request, forceInsecure, alwaysCreate)
                   .addCallback)
        @andThen
        def _(session):
            if session is None:
                return
            sessionID = session.identifier
            try:
                ip_address = (request.client.host or b"").decode("ascii")
            except:
                ip_address = u""
            @self._datastore.sql
            def touch(engine):
                address_family = (u"AF_INET6" if u":" in ip_address
                                  else u"AF_INET")
                last_used = datetime.utcnow()
                sip = self._session_ip_table
                return upsert(engine, sip,
                              dict(sessionID=sessionID,
                                   ip_address=ip_address,
                                   address_family=address_family),
                              dict(last_used=last_used))
            @touch.addCallback
            def andReturn(ignored):
                return session
            return andReturn
        @_.addCallback
        def showMe(result):
            return result
        return _



def openSessionStore(reactor, db_uri, component_creators=(),
                       procurer_from_store=SessionProcurer):
    """
    Open a session store, returning a procurer that can procure sessions from
    it.

    @param db_uri: an SQLAlchemy database URI.
    @type db_uri: L{str}

    @param procurer_from_store: A callable that takes an L{ISessionStore} and
        returns an L{ISessionProcurer}.
    @type procurer_from_store: L{callable}

    @return: L{Deferred} firing with L{ISessionProcurer}
    """
    datastore = AlchimiaDataStore.open(
        reactor, db_uri, [
            AlchimiaSessionStore, AccountBindingStorePlugin,
            lambda metadata, store: IPTrackingProcurer(
                metadata, store, procurer_from_store(next(
                    store.componentsProviding(ISessionStore)
                ))),
            AccountLoginAuthorizer,
        ] + list(component_creators)
    )
    return next(datastore.componentsProviding(ISessionProcurer))



def tables(**kw):
    """
    Take a mapping of table names to columns and return a callable that takes a
    transaction and metadata and then ensures those tables with those columns
    exist.

    This is a quick-start way to initialize your schema; any kind of
    application that has a longer maintenance cycle will need a more
    sophisticated schema-migration approach.
    """
    @inlineCallbacks
    def callme(transaction, metadata):
        # TODO: inspect information schema, verify tables exist, don't try to
        # create them otherwise.
        for k, v in kw.items():
            print("creating table", k)
            try:
                yield transaction.execute(
                    CreateTable(Table(k, metadata, *v))
                )
            except OperationalError as oe:
                print("failure initializing table", k, oe)
    return callme



def authorizerFor(authzn_for, schema=lambda txn, metadata: None):
    """
    Declare an SQL authorizer, implemented by a given function.  Used like so::

        @authorizerFor(Foo, tables(foo=[Column("bar", Unicode())]))
        def authorize_foo(metadata, datastore, session_store, transaction,
                          session):
            return Foo(metadata, metadata.tables["foo"])

    @param authzn_for: The type we are creating an authorizer for.

    @param schema: a callable that takes a transaction and metadata, and
        returns a L{Deferred} which fires when it's done initializing the
        schema on that transaction.  See L{tables} for a convenient way to
        specify that.

    @return: a decorator that can decorate a function with the signature
        C{(metadata, datastore, session_store, transaction, session)}
    """
    an_authzn = authzn_for
    def decorator(decorated):
        @implementer(ISQLAuthorizer, ISQLSchemaComponent)
        @attr.s
        class AnAuthorizer(object):
            metadata = attr.ib()
            datastore = attr.ib()

            authzn_for = an_authzn

            def initialize_schema(self, transaction):
                return schema(transaction, self.metadata)

            def authzn_for_session(self, session_store, transaction, session):
                return decorated(self.metadata, self.datastore, session_store,
                                 transaction, session)

        decorated.authorizer = AnAuthorizer
        return decorated
    return decorator
