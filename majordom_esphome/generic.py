"""Generic mapping for entities without a hand-written one, derived from `aioesphomeapi`'s own types.

A parameter is made for every field of the entity's state class (read-only, unless the entity's command accepts that
field on its own) and for every other command argument that can be sent on its own. Value types come from the
annotations: bool, int, float, str and the library's integer enums (labelled with their member names). Beyond single
fields:

- an integer state that is a bit mask of a `<Kind>StateFlag` (a water heater's ON, AWAY) becomes one bool per flag;
- a command that needs several arguments at once (a date: year, month, day) becomes one `struct` parameter whose
  sub-parameters are the arguments, readable and writable;
- an optional argument that can only be sent together with a required one (a lock's `code` next to its `command`)
  becomes a `none` command whose sub-parameters are both arguments, typed when the command is used and never stored.

Hand-written mappings (`mapper.py`) win where the generic result is not good enough for the user.
"""

import dataclasses
import inspect
import math
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from functools import cache
from types import UnionType
from typing import Any

import aioesphomeapi
from aioesphomeapi import COMPONENT_TYPE_TO_INFO, APIClient, EntityState, LightColorCapability, WaterHeaterFeature
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole, ParameterUnit

from .models import ESPhomeParameter, ParameterSpec

# No parameters make sense for these: a camera stream, one-shot events, raw infrared / radio frames
SKIPPED = {"camera", "event", "infrared", "radio_frequency"}
STATE_NOISE = {"key", "device_id", "missing_state"}
# ESPHome reports these as fractions (0-1); MajorDom expresses them as percentages
FRACTIONS = {"position", "tilt", "volume", "brightness", "white", "color_brightness"}
# Already percentages (0-100) in ESPHome
PERCENT_VALUES = {"current_humidity", "target_humidity"}
UNITS = {
    "color_temperature": ParameterUnit.mired,
    "current_humidity": ParameterUnit.percentage,
    "target_humidity": ParameterUnit.percentage,
}


def _caps(entity: Any) -> int:
    """ColorMode values are bit masks of LightColorCapability, e.g. 35 = ON_OFF | BRIGHTNESS | RGB."""
    mask = 0
    for mode in entity.supported_color_modes:
        mask |= int(mode)
    return mask


# What an entity can do is declared on its info, not on its state: a parameter is only made when the entity says it
# supports the field. Keyed by (component, field); fields without an entry are always made.
GATES: dict[tuple[str, str], Callable[[Any], bool]] = {
    ("fan", "speed_level"): lambda e: e.supports_speed,
    ("fan", "oscillating"): lambda e: e.supports_oscillation,
    ("fan", "direction"): lambda e: e.supports_direction,
    ("fan", "preset_mode"): lambda e: bool(e.supported_preset_modes),
    ("cover", "tilt"): lambda e: e.supports_tilt,
    ("cover", "stop"): lambda e: e.supports_stop,
    # cold/warm white lights are driven through the colour temperature, their two channels are an implementation detail
    ("light", "color_temperature"): lambda e: bool(
        _caps(e) & (LightColorCapability.COLOR_TEMPERATURE | LightColorCapability.COLD_WARM_WHITE)
    ),
    ("light", "white"): lambda e: bool(_caps(e) & LightColorCapability.WHITE),
    # the colour part's own brightness only matters when colour and white channels mix
    ("light", "color_brightness"): lambda e: (
        bool(_caps(e) & LightColorCapability.RGB)
        and bool(_caps(e) & (LightColorCapability.WHITE | LightColorCapability.COLD_WARM_WHITE))
    ),
    ("light", "effect"): lambda e: bool(e.effects),
    ("climate", "action"): lambda e: e.supports_action,
    ("climate", "fan_mode"): lambda e: bool(e.supported_fan_modes),
    ("climate", "swing_mode"): lambda e: bool(e.supported_swing_modes),
    ("climate", "preset"): lambda e: bool(e.supported_presets),
    ("climate", "custom_fan_mode"): lambda e: bool(e.supported_custom_fan_modes),
    ("climate", "custom_preset"): lambda e: bool(e.supported_custom_presets),
    ("climate", "current_humidity"): lambda e: e.supports_current_humidity,
    ("climate", "target_humidity"): lambda e: e.supports_target_humidity,
    ("water_heater", "current_temperature"): lambda e: bool(
        e.supported_features & WaterHeaterFeature.SUPPORTS_CURRENT_TEMPERATURE
    ),
    ("water_heater", "target_temperature"): lambda e: (
        bool(e.supported_features & WaterHeaterFeature.SUPPORTS_TARGET_TEMPERATURE)
        and not e.supported_features & WaterHeaterFeature.SUPPORTS_TWO_POINT_TARGET_TEMPERATURE
    ),
    ("water_heater", "target_temperature_low"): lambda e: bool(
        e.supported_features & WaterHeaterFeature.SUPPORTS_TWO_POINT_TARGET_TEMPERATURE
    ),
    ("water_heater", "target_temperature_high"): lambda e: bool(
        e.supported_features & WaterHeaterFeature.SUPPORTS_TWO_POINT_TARGET_TEMPERATURE
    ),
    ("water_heater", "mode"): lambda e: bool(e.supported_features & WaterHeaterFeature.SUPPORTS_OPERATION_MODE),
    ("water_heater", "away"): lambda e: bool(e.supported_features & WaterHeaterFeature.SUPPORTS_AWAY_MODE),
    ("water_heater", "on"): lambda e: bool(e.supported_features & WaterHeaterFeature.SUPPORTS_ON_OFF),
    # a command with a code is only offered where the entity asks for one
    ("lock", "command_with_code"): lambda e: e.requires_code,
    ("alarm_control_panel", "command_with_code"): lambda e: e.requires_code or e.requires_code_to_arm,
}
# String fields that are a choice from a list on the info: made as enums (index <-> option), like a select
OPTIONS: dict[tuple[str, str], Callable[[Any], list[str]]] = {
    ("fan", "preset_mode"): lambda e: list(e.supported_preset_modes),
    ("light", "effect"): lambda e: list(e.effects),
    ("climate", "custom_fan_mode"): lambda e: list(e.supported_custom_fan_modes),
    ("climate", "custom_preset"): lambda e: list(e.supported_custom_presets),
}
# Enum fields of which the entity supports only some members
SUBSETS: dict[tuple[str, str], Callable[[Any], Iterable[IntEnum]]] = {
    ("climate", "fan_mode"): lambda e: e.supported_fan_modes,
    ("climate", "swing_mode"): lambda e: e.supported_swing_modes,
    ("climate", "preset"): lambda e: e.supported_presets,
    ("water_heater", "mode"): lambda e: e.supported_modes,
}


def _range(low: float, high: float) -> tuple[float | None, float | None]:
    """Limits an entity reports, or none when it reports nothing (both zero)."""
    return (low, high) if high > low else (None, None)


# Limits the entity reports
LIMITS: dict[tuple[str, str], Callable[[Any], tuple[float | None, float | None]]] = {
    ("fan", "speed_level"): lambda e: (0, e.supported_speed_count),
    ("light", "color_temperature"): lambda e: (e.min_mireds, e.max_mireds),
    ("climate", "target_humidity"): lambda e: (e.visual_min_humidity, e.visual_max_humidity),
    ("water_heater", "target_temperature"): lambda e: _range(e.min_temperature, e.max_temperature),
    ("water_heater", "target_temperature_low"): lambda e: _range(e.min_temperature, e.max_temperature),
    ("water_heater", "target_temperature_high"): lambda e: _range(e.min_temperature, e.max_temperature),
}
# Bounds of calendar and clock fields, whatever the entity
FIELD_LIMITS: dict[str, tuple[float, float]] = {
    "month": (1, 12),
    "day": (1, 31),
    "hour": (0, 23),
    "minute": (0, 59),
    "second": (0, 59),
}


@dataclass(frozen=True)
class Field:
    name: str
    kind: type  # bool, int, float, str, an IntEnum, or dict for a group of arguments (see `children`)
    in_state: bool
    in_command: bool  # the command accepts it on its own
    is_flag: bool = False  # a command-only bool: "do it"
    bit: tuple[str, int] | None = None  # a flag inside an integer state field: (that field, the flag's mask)
    children: tuple["Field", ...] = ()  # the arguments sent together: a `struct` value or a command with arguments

    @property
    def is_group(self) -> bool:
        return bool(self.children)

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
    simple_required = all(command_kinds[name] is not None for name in required)

    # several required arguments that are also the state (a date: year, month, day): one struct value
    grouped: set[str] = set()
    result: list[Field] = []
    if len(required) > 1 and simple_required:
        children = tuple(Field(n, command_kinds[n] or int, n in state_kinds, True) for n in _ordered(command, required))
        result.append(Field(component, dict, all(c.in_state for c in children), True, children=children))
        grouped = set(required)

    # an integer state that is a bit mask of flags the command sets one by one (a water heater's ON, AWAY)
    flags = _flag_class(component)
    flagged: set[str] = set()
    for name, kind in state_kinds.items():
        if kind is int and flags is not None:
            for member in flags:
                flag = (member.name or "").lower()
                if command_kinds.get(flag) is bool:
                    result.append(Field(flag, bool, True, flag in sendable, bit=(name, int(member))))
                    flagged |= {name, flag}

    skip = grouped | flagged
    result += [
        Field(name, kind, True, name in sendable)
        for name, kind in state_kinds.items()
        if kind is not None and name not in skip
    ]
    for name in sendable - set(state_kinds) - skip:
        kind = command_kinds[name]
        assert kind is not None
        result.append(Field(name, kind, False, True, is_flag=kind is bool))

    # an optional argument that can only travel with the required one (a lock's code): a command with arguments
    if required and len(required) == 1 and simple_required:
        (main,) = required
        for name, kind in command_kinds.items():
            if name not in sendable and name not in required and kind is not None and name not in state_kinds:
                main_field = Field(main, command_kinds[main] or int, False, True)
                children = (main_field, Field(name, kind, False, True))
                result.append(Field(f"{main}_with_{name}", dict, False, True, children=children))
    return tuple(result)


def _ordered(command: Any, names: set[str]) -> list[str]:
    return [name for name in inspect.signature(command).parameters if name in names]


def _flag_class(component: str) -> type[IntFlag] | None:
    base = COMPONENT_TYPE_TO_INFO[component].__name__.removesuffix("Info")
    candidate = getattr(aioesphomeapi, f"{base}StateFlag", None)
    return candidate if isinstance(candidate, type) and issubclass(candidate, IntFlag) else None


def is_mapped(component: str) -> bool:
    return component not in SKIPPED and bool(fields_of(component))


def specs(component: str, entity: Any = None, skip: frozenset[str] = frozenset()) -> list[ParameterSpec]:
    """Parameters of the entity's fields, except `skip` (what a hand-written mapping already covers).

    With the entity, what it does not support is left out, enums are limited to what it supports, option lists
    become enums and the limits it reports are used.
    """
    result = []
    for field in fields_of(component):
        if field.name not in skip and (spec := _spec(component, field, entity)) is not None:
            result.append(spec)
    return result


def _spec(component: str, field: Field, entity: Any) -> ParameterSpec | None:
    key = (component, field.name)
    if entity is not None and key in GATES and not GATES[key](entity):
        return None
    role = ParameterRole.control if field.in_command else ParameterRole.sensor
    if field.is_group:
        children = tuple(spec for child in field.children if (spec := _spec(component, child, entity)) is not None)
        data_type = ParameterDataType.struct if field.in_state else ParameterDataType.none
        return ParameterSpec(field.name, data_type, role, fields=children)
    unit = UNITS.get(field.name, ParameterUnit.plain)
    low, high = (
        LIMITS[key](entity) if entity is not None and key in LIMITS else FIELD_LIMITS.get(field.name, (None, None))
    )
    if field.is_flag:
        return ParameterSpec(field.name, ParameterDataType.none, role)
    if entity is not None and key in OPTIONS:
        return ParameterSpec(
            field.name, ParameterDataType.enum, role, valid_values=dict(enumerate(OPTIONS[key](entity)))
        )
    if issubclass(field.kind, IntEnum):
        members = SUBSETS[key](entity) if entity is not None and key in SUBSETS else field.kind
        return ParameterSpec(field.name, ParameterDataType.enum, role, valid_values={int(m): m.name for m in members})
    if field.kind is bool:
        return ParameterSpec(field.name, ParameterDataType.bool, role)
    if field.kind is int:
        return ParameterSpec(field.name, ParameterDataType.integer, role, unit, low, high)
    if field.kind is float and field.fraction:
        return ParameterSpec(field.name, ParameterDataType.decimal, role, ParameterUnit.percentage, 0, 100)
    if field.kind is float:
        return ParameterSpec(field.name, ParameterDataType.decimal, role, unit, low, high)
    return ParameterSpec(field.name, ParameterDataType.string, role)


def state_values(
    component: str, state: EntityState, skip: frozenset[str] = frozenset(), entity: Any = None
) -> dict[str, Any]:
    """Current values by field name; a `struct` value is a dict of its fields by name."""
    if getattr(state, "missing_state", False):
        return {}
    values: dict[str, Any] = {}
    for field in fields_of(component):
        if not field.in_state or field.name in skip:
            continue
        if field.is_group:
            group = {
                child.name: _to_hub(component, child, getattr(state, child.name, None), entity)
                for child in field.children
            }
            if all(value is not None for value in group.values()):
                values[field.name] = group
            continue
        source = field.bit[0] if field.bit else field.name
        value = _to_hub(component, field, getattr(state, source, None), entity)
        if value is not None:
            values[field.name] = value
    return values


def _to_hub(component: str, field: Field, value: Any, entity: Any) -> Any:
    """A state value as the parameter's plain value, or None when there is nothing to report."""
    if value is None:
        return None
    key = (component, field.name)
    if field.bit is not None:
        return bool(int(value) & field.bit[1])
    if key in OPTIONS and entity is not None:
        options = OPTIONS[key](entity)
        return options.index(value) if value in options else None  # an empty choice: nothing selected
    if field.kind is float:
        if math.isnan(value):
            return None
        return round(value * 100, 2) if field.fraction else float(value)
    if field.kind is bool:
        return bool(value)
    if field.kind is str:
        return str(value)
    value = int(value)  # int and the library's integer enums: plain int
    if key in SUBSETS and entity is not None and value not in {int(m) for m in SUBSETS[key](entity)}:
        return None  # e.g. "no preset": not one of the choices the entity offers
    return value


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
    if field.is_group:
        return _group_args(component, field, parameter, value)
    return {name: _to_device(component, field, parameter, value)}


def _group_args(component: str, field: Field, parameter: ESPhomeParameter, value: Any) -> dict[str, Any]:
    """The arguments of a group: the value is a dict keyed by sub-parameter id (as the SDK specifies) or name."""
    if not isinstance(value, dict):
        raise ValueError(f"{parameter.name}: expected the values of {[c.name for c in field.children]}, got {value!r}")
    children = {sub.integration_data.sub_field: sub for sub in sub_parameters(parameter)}
    by_key = {str(key): item for key, item in value.items()}
    args = {}
    for child in field.children:
        sub = children.get(child.name)
        if sub is None:
            continue  # not offered by this entity
        raw = by_key.get(str(sub.id), by_key.get(child.name))
        if raw is None:
            raise ValueError(f"{parameter.name}: {child.name} is missing")
        args[child.name] = _to_device(component, child, sub, raw)
    return args


def sub_parameters(parameter: ESPhomeParameter) -> list[ESPhomeParameter]:
    """The parameter's sub-parameters, also when they come back from storage as plain dicts."""
    return [
        sub if isinstance(sub, ESPhomeParameter) else ESPhomeParameter.model_validate(sub)
        for sub in parameter.fields or []
    ]


def _to_device(component: str, field: Field, parameter: ESPhomeParameter, value: Any) -> Any:
    """A parameter value as the argument the library's command takes."""
    if field.is_flag:
        return True
    if (component, field.name) in OPTIONS:
        if value not in (parameter.valid_values or {}):
            raise ValueError(f"{parameter.name}: no option {value!r}")
        return (parameter.valid_values or {})[value]
    if issubclass(field.kind, IntEnum):
        if value not in (parameter.valid_values or {}):
            raise ValueError(f"{parameter.name}: no option {value!r}")
        return field.kind(value)
    if field.kind is bool:
        return as_bool(value)
    if field.kind is float:
        number = as_number(value, parameter)
        return number / 100 if field.fraction else number
    if field.kind is int:
        return int(as_number(value, parameter))
    return str(value)
