import abc
import json
import math
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Tuple, Dict, Any, List, Type

import aiofiles
from gcp_pilot.datastore import Document, DoesNotExist
from gcp_pilot.exceptions import ValidationError
from sanic import request, response, views

from sanic_rest import exceptions, validator

PayloadType = Dict[str, Any]
ResponseType = Tuple[PayloadType, int]

STAGE_DIR = os.environ.get("SANIC_FILE_DIR", default=Path(__file__).parent)


def get_model_or_404(model_klass: Type[Document], pk: str, query_filters: Dict = None) -> Document:
    query_filters = query_filters or {}
    try:
        return model_klass.documents.get(id=pk, **query_filters)
    except DoesNotExist as e:
        raise exceptions.NotFoundError() from e


class FileProcessingMixin:
    async def store_file(self, field_name: str, file: request.File) -> str:
        identifier = uuid.uuid4().hex
        filepath = Path("media") / identifier / field_name / file.name
        output = await self.write_file(file=file, filepath=filepath)
        return str(output)

    async def process_files(self, files: Dict[str, request.File]) -> Dict[str, Any]:
        file_updates: Dict[str, Any] = defaultdict(list)
        for key, key_files in files.items():
            for file in key_files:
                filepath = await self.store_file(
                    field_name=key,
                    file=file,
                )
                file_updates[key].append(filepath)
        return file_updates

    @classmethod
    async def write_file(cls, file: request.File, filepath: Path) -> str:
        filepath = STAGE_DIR / filepath
        filepath.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(filepath, "wb") as f:
            await f.write(file.body)
        await f.close()
        return str(filepath)


class ValidationMixin:
    def validate(self, data, model_klass=None, partial=False):
        model_klass = model_klass or self.model
        info = validator.ModelInfo.build(model_klass=model_klass)
        errors = info.validate(item=data, partial=partial)
        if errors:
            raise exceptions.ValidationError(message=errors)
        return data

    def _validate_item(self, item, options, partial=False) -> Dict[str, Any]:
        errors = {}

        for field_name, field_info in options["fields"]:
            if not partial and field_info["required"] and field_name not in item:
                errors[field_name] = f"Mandatory field"
                continue

            provided_items = item[field_name]

            if field_info["repeated"]:
                for provided_item in provided_items:
                    errors[field_name] = self._validate_item(item=provided_item, options=field_info, partial=False)

        return errors


class ViewBase(FileProcessingMixin, ValidationMixin, views.HTTPMethodView):
    model: Type[Document]

    def _parse_body(self, request: request.Request) -> Tuple[Dict[str, Any], Dict[str, request.File]]:
        if "form" in request.content_type:
            data = {}
            for key, value in dict(request.form).items():
                value = [json.loads(item) for item in value]
                if len(value) == 1:
                    value = value[0]
                data[key] = value
        else:
            data = request.json

        return data, dict(request.files)

    def _parse_pk(self, pk: str):
        pk_field_name = self.model.Meta.pk_field
        pk_field_type = self.model.Meta.fields[self.model.Meta.pk_field]
        try:
            return pk_field_type(pk)
        except ValueError as e:
            raise exceptions.ValidationError(f"{pk_field_name} field must be {pk_field_type.__name__}") from e

    def get_model(self, pk: str) -> Document:
        return get_model_or_404(model_klass=self.model, pk=self._parse_pk(pk))


class ListView(ViewBase, abc.ABC):
    search_field: str = None

    async def get(self, request: request.Request) -> response.HTTPResponse:
        query_args, page, page_size = self._parse_query_args(request=request)
        try:
            items = await self.perform_get(query_filters=query_args)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        items_in_page = self._paginate(items=items, page=page, page_size=page_size)

        response_body = {
            "results": [obj.serialize() for obj in items_in_page],
            "count": len(items),
            "num_pages": int(math.ceil(len(items) / page_size)),
        }
        return response.json(response_body, 200 if any(items_in_page) else 404)

    def _parse_query_args(self, request: request.Request) -> Tuple[Dict[str, Any], int, int]:
        query_args = dict()
        for key, value in request.query_args:
            if key == "q":
                key = f"{self.search_field}__startswith"

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
        except ValueError as e:
            raise exceptions.ValidationError(f"page and page_size must be integers") from e
        if page < 1:
            raise exceptions.ValidationError(f"page starts at 1")
        if page_size < 1:
            raise exceptions.ValidationError(f"page_size must be at least at 1")

        return query_args, page, page_size

    def _paginate(self, items: List[Document], page: int, page_size: int) -> List[Document]:
        # TODO Add proper pagination with cursors
        start_idx = (page - 1) * page_size
        start_idx = min(start_idx, len(items))

        end_idx = start_idx + page_size
        end_idx = min(end_idx, len(items))

        items_in_page = items[start_idx:end_idx]
        return items_in_page

    async def perform_get(self, query_filters) -> List[Document]:
        return list(self.model.documents.filter(**query_filters))

    async def post(self, request: request.Request) -> response.HTTPResponse:
        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data, partial=False)

        try:
            obj = await self.perform_create(data=validated_data, files=files)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        response_body = obj.serialize()
        return response.json(response_body, 201)

    async def perform_create(self, data: PayloadType, files: Dict[str, request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.deserialize(**data, **file_updates)
        return obj.save()

    async def options(self, request: request.Request) -> response.HTTPResponse:
        data = await self.perform_options()
        return response.json(data, 200)

    async def perform_options(self) -> PayloadType:
        return validator.ModelInfo.build(model_klass=self.model).as_dict


class DetailView(ViewBase, abc.ABC):
    async def get(self, request: request.Request, pk: str) -> response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        try:
            obj = await self.perform_get(obj=current_obj, query_filters={})
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        data = obj.serialize()
        return response.json(data, 200)

    async def perform_get(self, obj: Document, query_filters) -> Document:
        return obj

    async def put(self, request: request.Request, pk: str) -> response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data, partial=False)

        try:
            obj = await self.perform_create(obj=current_obj, data=validated_data, files=files)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        response_body = obj.serialize()
        return response.json(response_body, 200)

    async def perform_create(self, obj: Document, data: PayloadType, files: Dict[str, request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.deserialize(pk=obj.pk, **data, **file_updates)
        return obj.save()

    async def patch(self, request: request.Request, pk: str) -> response.HTTPResponse:
        current_obj = self.get_model(pk=pk)

        data, files = self._parse_body(request=request)
        validated_data = self.validate(data=data, partial=False)

        try:
            obj = await self.perform_update(obj=current_obj, data=validated_data, files=files)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        response_body = obj.serialize()
        return response.json(response_body, 200)

    async def perform_update(self, obj: Document, data: PayloadType, files: Dict[str, request.File]) -> Document:
        file_updates = await self.process_files(files=files)

        obj = self.model.documents.update(pk=obj.pk, **data, **file_updates)

        return obj

    async def delete(self, request: request.Request, pk: str) -> response.HTTPResponse:
        await self.perform_delete(pk=pk)
        return response.json({}, 204)

    async def perform_delete(self, pk: str) -> None:
        self.model.documents.delete(pk=pk)


class NestViewBase(ViewBase):
    nest_model: Type[Document]

    def get_nest_model(self, pk: str):
        return get_model_or_404(model_klass=self.nest_model, pk=pk)


class NestedListView(NestViewBase):
    async def get(self, request: request.Request, nest_pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_get(request=request, nest_obj=nest_obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_get(self, request: request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def post(self, request: request.Request, nest_pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_post(request=request, nest_obj=nest_obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_post(self, request: request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def put(self, request: request.Request, nest_pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        try:
            data, status = await self.perform_put(request=request, nest_obj=nest_obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_put(self, request: request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def delete(self, request: request.Request, nest_pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)

        data, status = await self.perform_delete(request=request, nest_obj=nest_obj)
        return response.json(data, status)

    @abc.abstractmethod
    async def perform_delete(self, request: request.Request, nest_obj: Document) -> ResponseType:
        raise NotImplementedError()


class NestedDetailView(NestViewBase):
    async def get(self, request: request.Request, nest_pk: str, pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_get(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_get(self, request: request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def post(self, request: request.Request, nest_pk: str, pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_post(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_post(self, request: request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def put(self, request: request.Request, nest_pk: str, pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        try:
            data, status = await self.perform_put(request=request, nest_obj=nest_obj, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return response.json(data, status)

    @abc.abstractmethod
    async def perform_put(self, request: request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def delete(self, request: request.Request, nest_pk: str, pk: str) -> response.HTTPResponse:
        nest_obj = self.get_nest_model(pk=nest_pk)
        obj = self.get_model(pk=pk)

        data, status = await self.perform_delete(request=request, nest_obj=nest_obj, obj=obj)
        return response.json(data, status)

    @abc.abstractmethod
    async def perform_delete(self, request: request.Request, nest_obj: Document, obj: Document) -> ResponseType:
        raise NotImplementedError()
