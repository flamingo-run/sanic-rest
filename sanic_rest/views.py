import abc
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Tuple, Dict, Any, List

import aiofiles
from gcp_pilot.datastore import Document, DoesNotExist
from gcp_pilot.exceptions import ValidationError
from sanic.request import Request, RequestParameters, File
from sanic.response import json, HTTPResponse
from sanic.views import HTTPMethodView

from sanic_rest import exceptions, validator

PayloadType = Dict[str, Any]
ResponseType = Tuple[PayloadType, int]

STAGE_DIR = os.environ.get('SANIC_FILE_DIR', default=Path(__file__).parent)


class ViewBase(HTTPMethodView):
    model: Document

    @classmethod
    async def write_file(cls, file: File, filepath: Path):
        filepath.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(filepath, 'wb') as f:
            await f.write(file.body)
        await f.close()

    def validate(self, data, model_klass=None, partial=False):
        model_klass = model_klass or self.model
        info = validator.ModelInfo.build(model_klass=model_klass)
        errors = info.validate(item=data, partial=partial)
        if errors:
            raise exceptions.ValidationError(message=errors)

    def _validate_item(self, item, options, partial=False) -> Dict[str, Any]:
        errors = {}

        for field_name, field_info in options['fields']:
            if not partial and field_info['required'] and field_name not in item:
                errors[field_name] = f"Mandatory field"
                continue

            provided_items = item[field_name]

            if field_info['repeated']:
                for provided_item in provided_items:
                    errors[field_name] = self._validate_item(item=provided_item, options=field_info, partial=False)

        return errors


class ListView(ViewBase, abc.ABC):
    search_field: str = None

    async def get(self, request: Request) -> HTTPResponse:
        query_args, page, page_size = self._parse_query_args(request=request)
        try:
            items = await self.perform_get(query_filters=query_args)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        items_in_page = self._paginate(items=items, page=page, page_size=page_size)

        response = {
            'results': [
                obj.serialize()
                for obj in
                items_in_page
            ],
            'count': len(items),
            'num_pages': int(math.ceil(len(items) / page_size))
        }
        return json(response, 200 if any(items_in_page) else 404)

    def _parse_query_args(self, request: Request) -> Tuple[Dict[str, Any], int, int]:
        query_args = dict()
        for key, value in request.query_args:
            if key == 'q':
                key = f'{self.search_field}__startswith'

            if key not in query_args:
                query_args[key] = value
            elif isinstance(query_args[key], list):
                query_args[key].append(value)
            else:
                query_args[key] = [query_args[key], value]

        # Fetch & validate pagination params
        try:
            page = int(query_args.pop('page', 1))
            page_size = int(query_args.pop('page_size', 10))
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

    async def post(self, request: Request) -> HTTPResponse:
        data = request.json
        self.validate(data=data, partial=False)

        try:
            obj = await self.perform_create(data=data)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        data = obj.serialize()
        return json(data, 201)

    async def perform_create(self, data: PayloadType) -> Document:
        obj = self.model.deserialize(**data)
        return obj.save()

    async def options(self, request: Request) -> HTTPResponse:
        data = await self.perform_options()
        return json(data, 200)

    async def perform_options(self) -> PayloadType:
        return validator.ModelInfo.build(model_klass=self.model).as_dict


class DetailView(ViewBase, abc.ABC):
    def _parse_pk(self, pk: str):
        pk_field_name = self.model.Meta.pk_field
        pk_field_type = self.model.Meta.fields[self.model.Meta.pk_field]
        try:
            return pk_field_type(pk)
        except ValueError as e:
            raise exceptions.ValidationError(f"{pk_field_name} field must be {pk_field_type.__name__}") from e

    def get_model(self, pk: str, query_filters: Dict = None) -> Document:
        query_filters = query_filters or {}
        try:
            return self.model.documents.get(id=self._parse_pk(pk=pk), **query_filters)
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e

    async def get(self, request: Request, pk: str) -> HTTPResponse:
        current_obj = self.get_model(pk=pk)

        try:
            obj = await self.perform_get(obj=current_obj, query_filters={})
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        data = obj.serialize()
        return json(data, 200)

    async def perform_get(self, obj: Document, query_filters) -> Document:
        return obj

    async def put(self, request: Request, pk: str) -> HTTPResponse:
        current_obj = self.get_model(pk=pk)

        payload = request.json
        self.validate(data=payload, partial=True)

        try:
            obj = await self.perform_create(obj=current_obj, data=payload)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        data = obj.serialize()
        return json(data, 200)

    async def perform_create(self, obj: Document, data: PayloadType) -> Document:
        obj = self.model.deserialize(pk=obj.pk, **data)
        return obj.save()

    async def patch(self, request: Request, pk: str) -> HTTPResponse:
        current_obj = self.get_model(pk=pk)

        if request.files:
            payload = {}
        else:
            payload = request.json
            self.validate(data=payload, partial=True)

        try:
            obj = await self.perform_update(obj=current_obj, data=payload, files=request.files)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        data = obj.serialize()
        return json(data, 200)

    async def perform_update(self, obj: Document, data: PayloadType, files: RequestParameters) -> Document:
        obj = self.model.documents.update(pk=obj.pk, **data)

        file_updates: Dict[str, Any] = defaultdict(list)
        for key, file in files.items():
            filepath = await self.store_file(
                obj=obj,
                field_name=key,
                file=file[0],  # TODO: check if it's possible to have more files
            )
            if '__' not in key:
                file_updates[key] = filepath
            else:
                key = key.split('__')[0]
                file_updates[key].append(filepath)
        obj = self.model.documents.update(pk=obj.pk, **file_updates)

        return obj

    async def store_file(self, obj: Document, field_name: str, file: File) -> str:
        local_filepath = STAGE_DIR / 'media' / obj.pk / field_name
        await self.write_file(file=file, filepath=local_filepath)
        return str(local_filepath)

    async def delete(self, request: Request, pk: str) -> HTTPResponse:
        await self.perform_delete(pk=pk)
        return json({}, 204)

    async def perform_delete(self, pk: str) -> None:
        self.model.documents.delete(pk=pk)


class ActionView(ViewBase):
    def get_model(self, pk: str) -> Document:
        return self.model.documents.get(id=pk)

    async def get(self, request: Request, pk: str) -> HTTPResponse:
        try:
            obj = self.get_model(pk=pk)
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e

        try:
            data, status = await self.perform_get(request=request, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return json(data, status)

    @abc.abstractmethod
    async def perform_get(self, request: Request, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def post(self, request: Request, pk: str) -> HTTPResponse:
        try:
            obj = self.get_model(pk=pk)
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e

        try:
            data, status = await self.perform_post(request=request, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return json(data, status)

    @abc.abstractmethod
    async def perform_post(self, request: Request, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def put(self, request: Request, pk: str) -> HTTPResponse:
        try:
            obj = self.get_model(pk=pk)
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e

        try:
            data, status = await self.perform_put(request=request, obj=obj)
        except ValidationError as e:
            raise exceptions.ValidationError(message=str(e))

        return json(data, status)

    @abc.abstractmethod
    async def perform_put(self, request: Request, obj: Document) -> ResponseType:
        raise NotImplementedError()

    async def delete(self, request: Request, pk: str) -> HTTPResponse:
        try:
            obj = self.get_model(pk=pk)
        except DoesNotExist as e:
            raise exceptions.NotFoundError() from e

        data, status = await self.perform_delete(request=request, obj=obj)
        return json(data, status)

    @abc.abstractmethod
    async def perform_delete(self, request: Request, obj: Document) -> ResponseType:
        raise NotImplementedError()
