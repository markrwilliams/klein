# -*- test-case-name: klein.test.test_plating -*-

"""
Templating wrapper support for Klein.
"""

from json import dumps

from six import text_type, integer_types

from twisted.internet.defer import inlineCallbacks, returnValue

from twisted.web.template import TagLoader, Element
from twisted.web.error import MissingRenderMethod

from .app import _call
from ._decorators import bindable, modified, original_name

def _should_return_json(request):
    """
    Should the given request result in a JSON entity-body?
    """
    return bool(request.args.get(b"json"))


def json_serialize(item):
    """
    A function similar to L{dumps}.
    """
    def helper(unknown):
        if isinstance(unknown, PlatedElement):
            return unknown._as_json()
        else:
            raise TypeError("{input} not JSON serializable"
                            .format(input=unknown))
    return dumps(item, default=helper)


def _extra_types(input):
    """
    Renderability for a few additional types.
    """
    if isinstance(input, (float,) + integer_types):
        return text_type(input)
    return input


class PlatedElement(Element):
    """
    The element type returned by L{Plating}.  This contains several utility
    renderers.
    """

    def __init__(self, slot_data, preloaded, renderers, bound_instance,
                 presentation_slots):
        """
        @param slot_data: A dictionary mapping names to values.

        @param preloaded: The pre-loaded data.
        """
        self.slot_data = slot_data
        self._renderers = renderers
        self._bound_instance = bound_instance
        self._presentation_slots = presentation_slots
        super(PlatedElement, self).__init__(
            loader=TagLoader(preloaded.fillSlots(
                **{k: _extra_types(v) for k, v in slot_data.items()}
            ))
        )


    def _as_json(self):
        """
        
        """
        json_data = self.slot_data.copy()
        for ignored in self._presentation_slots:
            json_data.pop(ignored, None)
        return json_data


    def lookupRenderMethod(self, name):
        """
        @return: a renderer.
        """
        if name in self._renderers:
            wrapped = self._renderers[name]
            @modified("plated render wrapper", wrapped)
            def renderWrapper(request, tag, *args, **kw):
                return _call(self._bound_instance, wrapped,
                             request, tag, *args, **kw)
            return renderWrapper
        if ":" not in name:
            raise MissingRenderMethod(self, name)
        slot, type = name.split(":", 1)

        def renderList(request, tag):
            for item in self.slot_data[slot]:
                yield tag.fillSlots(item=_extra_types(item))
        types = {
            "list": renderList,
        }
        if type in types:
            return types[type]
        else:
            raise MissingRenderMethod(self, name)


class Plating(object):
    """
    A L{Plating} is a container which can be used to generate HTML from data.

    Its name is derived both from tem-I{plating} and I{chrome plating}.
    """

    CONTENT = "klein:plating:content"

    def __init__(self, defaults=None, tags=None,
                 presentation_slots=frozenset()):
        """
        
        """
        self._defaults = {} if defaults is None else defaults
        self._loader = TagLoader(tags)
        self._presentation_slots = {self.CONTENT} | set(presentation_slots)
        self._renderers = {}

    def render(self, renderer):
        """
        
        """
        self._renderers[text_type(original_name(renderer))] = renderer
        return renderer

    def routed(self, routing, content_template):
        """
        
        """
        def mydecorator(method):
            loader = TagLoader(content_template)
            @modified("plating route renderer", method, routing)
            @bindable
            @inlineCallbacks
            def mymethod(instance, request, *args, **kw):
                data = yield _call(instance, method, request, *args, **kw)
                if _should_return_json(request):
                    json_data = self._defaults.copy()
                    json_data.update(data)
                    for ignored in self._presentation_slots:
                        json_data.pop(ignored, None)
                    text_type = u'json'
                    result = json_serialize(json_data)
                else:
                    data[self.CONTENT] = loader.load()
                    text_type = u'html'
                    result = self._elementify(instance, data)
                request.setHeader(
                    b'content-type', (u'text/{format}; charset=utf-8'
                                      .format(format=text_type)
                                      .encode("charmap"))
                )
                returnValue(result)
            return method
        return mydecorator

    def _elementify(self, instance, to_fill_with):
        """
        
        """
        slot_data = self._defaults.copy()
        slot_data.update(to_fill_with)
        [loaded] = self._loader.load()
        loaded = loaded.clone()
        return PlatedElement(slot_data=slot_data,
                             preloaded=loaded,
                             renderers=self._renderers,
                             bound_instance=instance,
                             presentation_slots=self._presentation_slots)

    @classmethod
    def widget(cls, **kw):
        self = cls(**kw)
        def enwidget(function):
            @modified("Plating.widget renderer", function)
            @bindable
            def wrapper(instance, *a, **k):
                data = _call(instance, function, *a, **k)
                return self._elementify(instance, data)
            function.widget = wrapper
            return function
        return enwidget
