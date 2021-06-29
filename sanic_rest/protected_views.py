from abc import ABC

from sanic_jwt import protected

from sanic_rest.views import ListView, DetailView, NestedListView, NestedDetailView


class ProtectedMixin:
    decorators = [protected()]


class ProtectedListView(ProtectedMixin, ListView, ABC):
    pass


class ProtectedDetailView(ProtectedMixin, DetailView, ABC):
    pass


class ProtectedNestListView(ProtectedMixin, NestedListView, ABC):
    pass


class ProtectedNestDetailView(ProtectedMixin, NestedDetailView, ABC):
    pass
