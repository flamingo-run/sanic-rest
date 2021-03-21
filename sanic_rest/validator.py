from enum import Enum
import inspect
from dataclasses import _MISSING_TYPE, dataclass
from typing import Any, List, Dict, _GenericAlias, Union

from gcp_pilot.datastore import EmbeddedDocument

AnyInfoType = Union['FieldInfo', 'ModelInfo']


@dataclass
class FieldInfo:
    type: str
    required: bool

    @classmethod
    def build(cls, field_klass, required: bool) -> 'FieldInfo':
        if inspect.isclass(field_klass):
            field_type = field_klass.__name__
            if issubclass(field_klass, EmbeddedDocument):
                return ModelInfo.build(model_klass=field_klass, required=required)
            if issubclass(field_klass, Enum):
                return OptionsFieldInfo(type=field_type, required=required, choices=[e.value for e in field_klass])
            return FieldInfo(type=field_type, required=required)
        elif isinstance(field_klass, _GenericAlias):
            field_type = field_klass._name
            if field_type == 'List':
                inner_klass = field_klass.__args__[0]
                return ListFieldInfo(
                    type=field_type,
                    required=required,
                    struct=cls.build(field_klass=inner_klass, required=True),
                )
            elif field_type == 'Tuple':
                return TupleFieldInfo(
                    type=field_type,
                    required=required,
                    structs=[
                        cls.build(field_klass=inner_klass, required=True)
                        for inner_klass in field_klass.__args__
                    ],
                )
            elif field_type == 'Dict':
                key_klass, value_klass = field_klass.__args__
                return DictFieldInfo(
                    type=field_type,
                    required=required,
                    key_struct=cls.build(field_klass=key_klass, required=True),
                    value_struct=cls.build(field_klass=value_klass, required=True),
                )
            else:
                raise RuntimeError(f"Unable to handle field class {field_klass}")
        elif field_klass == Any:  # pylint: disable=comparison-with-callable
            return FieldInfo(type='any', required=required)
        else:
            raise RuntimeError(f"Unable to handle field class {field_klass}")

    def validate(self, item: Dict[Any, Any]) -> Union[str, None]:
        if self.required and item is None:
            return "Must not be empty"
        if isinstance(item, dict) and self.type != 'dict':
            return f"Must be a {self.type}"
        return None

    @property
    def as_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type,
            'required': self.required,
        }


@dataclass
class OptionsFieldInfo(FieldInfo):
    choices: List[Any]

    @property
    def as_dict(self) -> Dict[str, Any]:
        data = super().as_dict
        data['choices'] = self.choices
        return data

    def validate(self, item: Dict[Any, Any]) -> Union[str, None]:
        error = super().validate(item=item)
        if error:
            return error

        if item not in self.choices:
            return f"It must be one of {self.choices}. Not {item}"

        return None


@dataclass
class ModelInfo(FieldInfo):
    fields: Dict[str, FieldInfo]

    @classmethod
    def build(cls, model_klass: EmbeddedDocument, required=True) -> 'ModelInfo':
        hints = model_klass.__dataclass_fields__

        fields = {}
        for field_name, _ in model_klass.Meta.fields.items():
            hint = hints[field_name]
            field_required = isinstance(hint.default, _MISSING_TYPE) and isinstance(hint.default_factory, _MISSING_TYPE)
            field_klass = hint.type

            info = FieldInfo.build(field_klass=field_klass, required=field_required)
            fields[field_name] = info
        return cls(type=model_klass.__name__, fields=fields, required=required)

    def validate(self, item: Dict[Any, Any], partial: bool = False) -> Union[str, Dict[str, Any]]:
        errors = {}

        if not isinstance(item, dict):
            return f"Must be key-value ({self.type})"

        for field_name, field_info in self.fields.items():
            if field_name not in item:
                if not partial and field_info.required:
                    errors[field_name] = f"This field is required"
                continue

            field_errors = field_info.validate(item=item[field_name])
            if field_errors:
                errors[field_name] = field_errors

        return errors

    @property
    def as_dict(self):
        data = super().as_dict
        data['fields'] = {key: value.as_dict for key, value in self.fields.items()}
        return data


@dataclass
class ListFieldInfo(FieldInfo):
    struct: AnyInfoType

    def validate(self, item):
        if not isinstance(item, list):
            return 'Must be a list'

        errors = {}
        for idx, inner_item in enumerate(item):
            inner_error = self.struct.validate(item=inner_item)
            if inner_error:
                errors[f'#{idx}'] = inner_error
        return errors

    @property
    def as_dict(self) -> Dict[str, Any]:
        data = super().as_dict
        data['struct'] = self.struct.as_dict
        return data


@dataclass
class TupleFieldInfo(FieldInfo):
    structs: List[AnyInfoType]

    def validate(self, item):
        if not isinstance(item, (list, tuple)):
            return 'Must be a list'
        elif len(item) != len(self.structs):
            return f'Must be a list of {len(self.structs)} items'

        errors = {}
        for idx, (inner_item, struct) in enumerate(zip(item, self.structs)):
            inner_error = struct.validate(item=inner_item)
            if inner_error:
                errors[f'#{idx}'] = inner_error
        return errors

    @property
    def as_dict(self) -> Dict[str, Any]:
        data = super().as_dict
        data['structs'] = [struct.as_dict for struct in self.structs]
        return data


@dataclass
class DictFieldInfo(FieldInfo):
    key_struct: AnyInfoType
    value_struct: AnyInfoType

    def validate(self, item):
        if not isinstance(item, dict):
            return 'Must be key-value'

        errors = {}
        for idx, (item_key, item_value) in enumerate(item.items()):
            item_error = self.key_struct.validate(item_key)
            if item_error:
                errors[f'#{idx}_KEY'] = item_error

            value_error = self.value_struct.validate(item_value)
            if value_error:
                errors[f'#{idx}_VALUE'] = value_error
        return errors

    @property
    def as_dict(self) -> Dict[str, Any]:
        data = super().as_dict
        data['key_struct'] = self.key_struct.as_dict
        data['value_struct'] = self.value_struct.as_dict
        return data
