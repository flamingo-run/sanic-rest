import abc
import inspect
import json
import math
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiofiles
import pydantic
import sanic.request
import sanic.views
from gcp_pilot.datastore import (
    DEFAULT_PK_FIELD_NAME,
    DEFAULT_PK_FIELD_TYPE,
    Document,
    DoesNotExist,
    EmbeddedDocument,
)
from gcp_pilot.exceptions import ValidationError

from sanic_rest import exceptions

PayloadType = dict[str, Any]
ResponseType = tuple[PayloadType, int]

STAGE_DIR = os.environ.get("SANIC_FILE_DIR", default=Path(__file__).parent)


def get_model_or_404(model_klass: type[Document], pk: Any, query_filters: dict | None = None) -> Document:
    query_filters = query_filters or {}
    try:
        return model_klass.documents.get(id=pk, **query_filters)
    except DoesNotExist as exc:
        raise exceptions.NotFoundError() from exc


class FileProcessingMixin:
    async def store_file(self, field_name: str, file: sanic.request.File) -> str:
        identifier = uuid.uuid4().hex
        filepath = Path("media") / identifier / field_name / file.name
        output = await self.write_file(file=file, filepath=filepath)
        return str(output)

    async def process_files(self, files: dict[str, sanic.request.File]) -> dict[str, Any]:
        file_updates: dict[str, Any] = defaultdict(list)
        for key, key_files in files.items():
            for file in key_files:
                filepath = await self.store_file(
                    field_name=key,
                    file=file,
                )
                file_updates[key].append(filepath)
        return file_updates

    @classmethod
    async def write_file(cls, file: sanic.request.File, filepath: Path) -> str:
        filepath = STAGE_DIR / filepath
        filepath.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(filepath, "wb") as file:
            await file.write(file.body)
        await file.close()
        return str(filepath)


class ValidationMixin:
    def validate(self, data, model_klass: type[Document] | None = None, current_obj: Document | None = None):
        model_klass = model_klass or self.model

        model_data = (current_obj.dict() if current_obj else {}) | data
        try:
            model_klass(**model_data)
        except pydantic.ValidationError as exc:
            field_errors = {}
            for err in exc.errors():
                for field in err["loc"]:
                    field_errors[field] = err["msg"]
            raise exceptions.ValidationError(message=field_errors) from exc
        return data


class ViewBase(FileProcessingMixin, ValidationMixin, sanic.views.HTTPMethodView):
    model: type[Document]

    def _parse_body(self, request: sanic.request.Request) -> tuple[dict[str, Any], dict[str, sanic.request.File]]:
        if "form" in request.content_type:
            data = {}
            for key, form_value in dict(request.form).items():
                value = [json.loads(item) for item in form_value]
                if len(value) == 1:
                    value = value[0]
                data[key] = value
        else:
            data = request.json

        return data, dict(request.files)

    def _parse_pk(self, pk: str):
        pk_field_name = DEFAULT_PK_FIELD_NAME
        pk_field_type = DEFAULT_PK_FIELD_TYPE
        try:
            return pk_field_type(pk)
        except ValueError as exc:
            raise exceptions.ValidationError(f"{pk_field_name} field must be {pk_field_type.__name__}") from exc

    def get_model(self, pk: str) -> Document:
        return get_model_or_404(model_klass=self.model, pk=self._parse_pk(pk))


class ListView(ViewBase, abc.ABC):
    search_field: str = None

    async def get(self, request: sanic.request.Request) -> sanic.response.HTTPResponse:
        query_args, page, page_size = self._parse_query_args(request=request)
        try:
            items = await self.perform_get(query_filters=query_args)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        items_in_page = self._paginate(items=items, page=page, page_size=page_size)

        response_body = {
            "results": [obj.to_dict() for obj in items_in_page],
            "count": len(items),
            "num_pages": int(math.ceil(len(items) / page_size)),
        }
        return sanic.response.json(response_body, 200 if any(items_in_page) else 404, default=str)

    def _parse_query_args(self, request: sanic.request.Request) -> tuple[dict[str, Any], int, int]:
        query_args = {}
        for q_key, value in request.query_args:
            key = f"{self.search_field}__startswith" if q_key == "q" else q_key

            if key not in query_args:
                query_args[key] = value
            elif isinstance(query_args[key], list):
                query_args[key].append(value)
            else:
                query_args[key] = [query_args[key], value]

        # Fetch & validate pagination params
        try:
            page = int(query_args.pop("page", 1))
            page_size = int(query_args.pop("page_size", 10))
        except ValueError as exc:
            raise exceptions.ValidationError("page and page_size must be integers") from exc
        if page < 1:
            raise exceptions.ValidationError("page starts at 1")
        if page_size < 1:
            raise exceptions.ValidationError("page_size must be at least at 1")

        return query_args, page, page_size

    def _paginate(self, items: list[Document], page: int, page_size: int) -> list[Document]:
        # TODO Add proper pagination with cursors
        start_idx = (page - 1) * page_size
        start_idx = min(start_idx, len(items))

        end_idx = start_idx + page_size
        end_idx = min(end_idx, len(items))

        items_in_page = items[start_idx:end_idx]
        return items_in_page

    async def perform_get(self, query_filters) -> list[Document]:
        return list(self.model.documents.filter(**query_filters))

    async def post(self, request: sanic.request.Request) -> sanic.response.HTTPResponse:
        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data)

        try:
            obj = await self.perform_create(data=validated_data, files=files)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        response_body = obj.to_dict()
        return sanic.response.json(response_body, 201, default=str)

    async def perform_create(self, data: PayloadType, files: dict[str, sanic.request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.from_dict(**data, **file_updates)
        return obj.save()

    async def options(self, request: sanic.request.Request) -> sanic.response.HTTPResponse:
        data = await self.perform_options()
        return sanic.response.json(data, 200, default=str)

    async def perform_options(self) -> PayloadType:
        def _get_options(klass):
            payload = {}
            for field_name, field_info in klass.__fields__.items():
                if inspect.isclass(field_info.type_) and issubclass(field_info.type_, EmbeddedDocument):
                    field_type = "struct"
                    extra = {"struct": _get_options(klass=field_info.type_)}
                else:
                    field_type = field_info.type_.__name__
                    extra = {}
                payload[field_name] = {"required": field_info.required, "type": field_type, **extra}

            return payload

        return _get_options(klass=self.model)


class DetailView(ViewBase, abc.ABC):
    async def get(self, request: sanic.request.Request, pk: str) -> sanic.response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        try:
            obj = await self.perform_get(obj=current_obj, query_filters={})
        except DoesNotExist as exc:
            raise exceptions.NotFoundError() from exc
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        data = obj.to_dict()
        return sanic.response.json(data, 200, default=str)

    async def perform_get(self, obj: Document, query_filters) -> Document:
        return obj

    async def put(self, request: sanic.request.Request, pk: str) -> sanic.response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data)

        try:
            obj = await self.perform_create(obj=current_obj, data=validated_data, files=files)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        response_body = obj.to_dict()
        return sanic.response.json(response_body, 200, default=str)

    async def perform_create(self, obj: Document, data: PayloadType, files: dict[str, sanic.request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.from_dict(pk=obj.pk, **data, **file_updates)
        return obj.save()

    async def patch(self, request: sanic.request.Request, pk: str) -> sanic.response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data, current_obj=current_obj)

        try:
            obj = await self.perform_update(obj=current_obj, data=validated_data, files=files)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        response_body = obj.to_dict()
        return sanic.response.json(response_body, 200, default=str)

    async def perform_update(self, obj: Document, data: PayloadType, files: dict[str, sanic.request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.documents.update(pk=obj.pk, **data, **file_updates)

        return obj

    async def delete(self, request: sanic.request.Request, pk: str) -> sanic.response.HTTPResponse:
        await self.perform_delete(pk=pk)
        return sanic.response.json({}, 204, default=str)

    async def perform_delete(self, pk: str) -> None:
        self.model.documents.delete(pk=pk)


class NestViewBase(ViewBase):
    nest_model: type[Document]

    def get_nest_model(self, pk: str):
        return get_model_or_404(model_klass=self.nest_model, pk=pk)


class NestedListView(NestViewBase):
    async def get(self, request: sanic.request.Request, nest_pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_get(request=request, nest_obj=nest_obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_get(self, request: sanic.request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def post(self, request: sanic.request.Request, nest_pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_post(request=request, nest_obj=nest_obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_post(self, request: sanic.request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def put(self, request: sanic.request.Request, nest_pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_put(request=request, nest_obj=nest_obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_put(self, request: sanic.request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def delete(self, request: sanic.request.Request, nest_pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        data, status = await self.perform_delete(request=request, nest_obj=nest_obj)
        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_delete(self, request: sanic.request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()


class NestedDetailView(NestViewBase):
    async def get(self, request: sanic.request.Request, nest_pk: str, pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_get(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_get(self, request: sanic.request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def post(self, request: sanic.request.Request, nest_pk: str, pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_post(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_post(self, request: sanic.request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def put(self, request: sanic.request.Request, nest_pk: str, pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_put(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as exc:
            raise exceptions.ValidationError(message=str(exc)) from exc

        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_put(self, request: sanic.request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def delete(self, request: sanic.request.Request, nest_pk: str, pk: str) -> sanic.response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        data, status = await self.perform_delete(request=request, nest_obj=nest_obj, obj=obj)
        return sanic.response.json(data, status, default=str)

    @abc.abstractmethod
    async def perform_delete(self, request: sanic.request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()
