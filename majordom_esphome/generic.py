"""Generic mapping for entities without a hand-written one, derived from `aioesphomeapi`'s own types.

A parameter is made for every field of the entity's state class (read-only, unless the entity's command accepts that
field on its own) and for every other command argument that can be sent on its own. Value types come from the
annotations: bool, int, float, str and the library's integer enums (labelled with their member names).

Hand-written mappings (`mapper.py`) win where the generic result is not good enough for the user.
"""

import dataclasses
import inspect
import math
import typing
from dataclasses import dataclass
from enum import IntEnum
from functools import cache
from types import UnionType
from typing import Any

import aioesphomeapi
from aioesphomeapi import COMPONENT_TYPE_TO_INFO, APIClient, EntityState
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole, ParameterUnit

from .models import ESPhomeParameter, ParameterSpec

# No parameters make sense for these: a camera stream, one-shot events, raw infrared / radio frames
SKIPPED = {"camera", "event", "infrared", "radio_frequency"}
STATE_NOISE = {"key", "device_id", "missing_state"}
# ESPHome reports these as fractions (0-1); MajorDom expresses them as percentages
FRACTIONS = {"position", "tilt", "volume", "brightness"}


@dataclass(frozen=True)
class Field:
    name: str
    kind: type  # bool, int, float, str or an IntEnum
    in_state: bool
    in_command: bool  # the command accepts it on its own
    is_flag: bool = False  # a command-only bool: "do it"

    @property
    def fraction(self) -> bool:
        return self.kind is float and self.name in FRACTIONS


def _hints(obj: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(obj)
    except Exception:  # annotations that only resolve inside the library's own namespace
        return {
            name: eval(annotation, vars(aioesphomeapi)) if isinstance(annotation, str) else annotation
            for name, annotation in getattr(obj, "__annotations__", {}).items()
        }


def _kind(annotation: Any) -> type | None:
    if typing.get_origin(annotation) in (typing.Union, UnionType):
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
    else:
        options = [annotation]
    if len(options) != 1:
        return None
    kind = options[0]
    if kind in (bool, int, float, str):
        return kind
    if isinstance(kind, type) and issubclass(kind, IntEnum):
        return kind
    return None


def _state_class(component: str) -> type[EntityState] | None:
    base = COMPONENT_TYPE_TO_INFO[component].__name__.removesuffix("Info")
    for name in (f"{base}State", f"{base}EntityState"):
        candidate = getattr(aioesphomeapi, name, None)
        if dataclasses.is_dataclass(candidate) and issubclass(candidate, EntityState):
            return candidate
    return None


def api_fields(component: str) -> tuple[set[str], set[str]]:
    """Every field of the entity's state and every argument of its command, whether or not it is mapped."""
    state_class = _state_class(component)
    state = (
        {f.name for f in dataclasses.fields(typing.cast(Any, state_class)) if f.name not in STATE_NOISE}
        if state_class is not None
        else set()
    )
    command = getattr(APIClient, f"{component}_command", None)
    arguments = (
        {name for name in inspect.signature(command).parameters if name not in ("self", "key", "device_id")}
        if command is not None
        else set()
    )
    return state, arguments


@cache
def fields_of(component: str) -> tuple[Field, ...]:
    state_class = _state_class(component)
    state_kinds: dict[str, type | None] = {}
    if state_class is not None:
        hints = _hints(state_class)
        state_kinds = {
            f.name: _kind(hints.get(f.name))
            for f in dataclasses.fields(typing.cast(Any, state_class))
            if f.name not in STATE_NOISE
        }

    command = getattr(APIClient, f"{component}_command", None)
    command_kinds: dict[str, type | None] = {}
    required: set[str] = set()
    if command is not None:
        hints = _hints(command)
        for name, parameter in inspect.signature(command).parameters.items():
            if name in ("self", "key", "device_id"):
                continue
            command_kinds[name] = _kind(hints.get(name))
            if parameter.default is inspect.Parameter.empty:
                required.add(name)
    # an argument can only be sent alone if it is the only one the command insists on
    sendable = {name for name, kind in command_kinds.items() if kind is not None and required <= {name}}

    result = [Field(name, kind, True, name in sendable) for name, kind in state_kinds.items() if kind is not None]
    for name in sendable - set(state_kinds):
        kind = command_kinds[name]
        assert kind is not None
        result.append(Field(name, kind, False, True, is_flag=kind is bool))
    return tuple(result)


def is_mapped(component: str) -> bool:
    return component not in SKIPPED and bool(fields_of(component))


def specs(component: str) -> list[ParameterSpec]:
    result = []
    for field in fields_of(component):
        role = ParameterRole.control if field.in_command else ParameterRole.sensor
        if field.is_flag:
            result.append(ParameterSpec(field.name, ParameterDataType.none, role))
        elif issubclass(field.kind, IntEnum):
            labels = {int(member): member.name for member in field.kind}
            result.append(ParameterSpec(field.name, ParameterDataType.enum, role, valid_values=labels))
        elif field.kind is bool:
            result.append(ParameterSpec(field.name, ParameterDataType.bool, role))
        elif field.kind is int:
            result.append(ParameterSpec(field.name, ParameterDataType.integer, role))
        elif field.kind is float and field.fraction:
            result.append(ParameterSpec(field.name, ParameterDataType.decimal, role, ParameterUnit.percentage, 0, 100))
        elif field.kind is float:
            result.append(ParameterSpec(field.name, ParameterDataType.decimal, role))
        else:
            result.append(ParameterSpec(field.name, ParameterDataType.string, role))
    return result


def state_values(component: str, state: EntityState) -> dict[str, Any]:
    if getattr(state, "missing_state", False):
        return {}
    values: dict[str, Any] = {}
    for field in fields_of(component):
        value = getattr(state, field.name, None) if field.in_state else None
        if value is None:
            continue
        if field.kind is float:
            if math.isnan(value):
                continue
            value = round(value * 100, 2) if field.fraction else float(value)
        elif field.kind is bool:
            value = bool(value)
        elif field.kind is str:
            value = str(value)
        else:  # int and the library's integer enums: plain int
            value = int(value)
        values[field.name] = value
    return values


def as_bool(value: Any) -> bool:
    if value not in (True, False):  # also accepts 0 and 1
        raise ValueError(f"Expected a boolean, got {value!r}")
    return bool(value)


def as_number(value: Any, parameter: ESPhomeParameter) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc
    low, high = parameter.min_value, parameter.max_value
    if (low is not None and number < low) or (high is not None and number > high):
        raise ValueError(f"{parameter.name}: {number} is outside {low}..{high}")
    return number


def command_args(component: str, parameter: ESPhomeParameter, value: Any) -> dict[str, Any]:
    """Keyword arguments (without the key) of the `<component>_command` method that applies `value`."""
    name = parameter.integration_data.sub_field or ""
    field = next((f for f in fields_of(component) if f.name == name and f.in_command), None)
    if field is None:
        raise ValueError(f"{parameter.name} cannot be set")
    if field.is_flag:
        return {name: True}
    if issubclass(field.kind, IntEnum):
        if value not in (parameter.valid_values or {}):
            raise ValueError(f"{parameter.name}: no option {value!r}")
        return {name: field.kind(value)}
    if field.kind is bool:
        return {name: as_bool(value)}
    if field.kind is float:
        number = as_number(value, parameter)
        return {name: number / 100 if field.fraction else number}
    if field.kind is int:
        return {name: int(as_number(value, parameter))}
    return {name: str(value)}
