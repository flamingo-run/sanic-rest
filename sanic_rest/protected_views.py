from abc import ABC

from sanic_jwt import protected

from sanic_rest.views import ListView, DetailView, ActionView


class ProtectedMixin:
    decorators = [protected()]


class ProtectedListView(ProtectedMixin, ListView, ABC):
    pass


class ProtectedDetailView(ProtectedMixin, DetailView, ABC):
    pass


class ProtectedActionView(ProtectedMixin, ActionView, ABC):
    pass
