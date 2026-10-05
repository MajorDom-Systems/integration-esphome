"""Translation between ESPHome entities/states/commands and MajorDom parameters/values/commands."""

import colorsys
import math
from collections.abc import Mapping
from dataclasses import replace
from enum import IntEnum
from typing import Any

from aioesphomeapi import (
    COMPONENT_TYPE_TO_INFO,
    BinarySensorState,
    ClimateInfo,
    ClimateMode,
    ClimateState,
    CoverInfo,
    CoverState,
    EntityCategory,
    EntityInfo,
    EntityState,
    FanState,
    LightColorCapability,
    LightInfo,
    LightState,
    NumberInfo,
    NumberState,
    SelectInfo,
    SelectState,
    SensorInfo,
    SensorState,
    SwitchState,
    TextSensorState,
)
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)

from . import generic
from .esphome_spec import get_unit
from .generic import as_bool as _as_bool
from .generic import as_number as _as_number
from .models import ESPhomeComponentType, ESPhomeParameter, ESPhomeParameterType, ParameterSpec

# The library's own registry covers every entity ESPHome knows; only the types in ESPhomeComponentType are mapped.
_COMPONENT_BY_INFO: dict[type[EntityInfo], str] = {info: name for name, info in COMPONENT_TYPE_TO_INFO.items()}

PERCENT = ParameterUnit.percentage  # MajorDom expresses levels, positions and saturation as 0-100


PARAMETER_TYPE: dict[ESPhomeComponentType, ESPhomeParameterType] = {
    ESPhomeComponentType.SENSOR: ESPhomeParameterType.SENSOR,
    ESPhomeComponentType.BINARY_SENSOR: ESPhomeParameterType.SENSOR,
    ESPhomeComponentType.TEXT_SENSOR: ESPhomeParameterType.SENSOR,
    ESPhomeComponentType.NUMBER: ESPhomeParameterType.NUMBER,
    ESPhomeComponentType.SELECT: ESPhomeParameterType.SELECT,
    ESPhomeComponentType.BUTTON: ESPhomeParameterType.BUTTON,
}

COVER_OPERATIONS = {
    0: "IDLE",
    1: "IS_OPENING",
    2: "IS_CLOSING",
}  # labels are the enum names, as in the other integrations


_HAND_WRITTEN = {component.value for component in ESPhomeComponentType}

# Fields of the state and command that the hand-written mapping covers (under the same or another name) or that are
# no values; the generic mapping adds every other field the entity supports, as `setting` parameters.
HANDLED: dict[str, frozenset[str]] = {
    "binary_sensor": frozenset({"state"}),
    "sensor": frozenset({"state"}),
    "text_sensor": frozenset({"state"}),
    "switch": frozenset({"state"}),
    "number": frozenset({"state"}),
    "select": frozenset({"state"}),
    "button": frozenset(),
    "fan": frozenset({"state", "speed"}),  # `speed` is the legacy three-step enum, speed_level replaces it
    "cover": frozenset({"position", "current_operation", "legacy_state"}),
    "light": frozenset(
        {"state", "brightness", "red", "green", "blue", "rgb", "color_mode"}
        | {"cold_white", "warm_white"}  # the channels behind color_temperature
        | {"flash_length", "transition_length"}  # the flash command and the transition setting below
    ),
    "climate": frozenset(
        {"mode", "current_temperature", "target_temperature", "target_temperature_low", "target_temperature_high"}
        | {"unused_legacy_away"}
    ),
}


def component_type_of(entity: EntityInfo) -> str | None:
    """The library's name of an entity's kind, or None if it has nothing to map (a camera, an event, ...)."""
    component = _COMPONENT_BY_INFO.get(type(entity))
    if component in _HAND_WRITTEN or (component is not None and generic.is_mapped(component)):
        return component
    return None


def visibility_of(entity: EntityInfo) -> ParameterVisibility:
    if entity.disabled_by_default:
        return ParameterVisibility.system
    if entity.entity_category != EntityCategory.NONE:  # configuration and diagnostics
        return ParameterVisibility.setting
    return ParameterVisibility.user


def parameter_specs(entity: EntityInfo, component: str) -> list[ParameterSpec]:
    if component not in _HAND_WRITTEN:
        return generic.specs(component, entity)
    extra = generic.specs(component, entity, skip=HANDLED[component])
    return _hand_written_specs(entity, ESPhomeComponentType(component)) + [
        replace(spec, visibility=ParameterVisibility.setting) for spec in extra
    ]


def _hand_written_specs(entity: EntityInfo, component: ESPhomeComponentType) -> list[ParameterSpec]:
    control, sensor = ParameterRole.control, ParameterRole.sensor
    decimal, boolean = ParameterDataType.decimal, ParameterDataType.bool
    unit = get_unit(getattr(entity, "device_class", None), getattr(entity, "unit_of_measurement", None))

    match component, entity:
        case ESPhomeComponentType.SWITCH | ESPhomeComponentType.FAN, _:
            return [ParameterSpec("state", boolean, control)]
        case ESPhomeComponentType.BINARY_SENSOR, _:
            return [ParameterSpec("state", boolean, sensor)]
        case ESPhomeComponentType.TEXT_SENSOR, _:
            return [ParameterSpec("state", ParameterDataType.string, sensor)]
        case ESPhomeComponentType.SENSOR, SensorInfo():
            return [ParameterSpec("state", decimal, sensor, unit)]
        case ESPhomeComponentType.BUTTON, _:
            return [ParameterSpec("state", ParameterDataType.none, control)]
        case ESPhomeComponentType.NUMBER, NumberInfo():
            return [
                ParameterSpec("state", decimal, control, unit, entity.min_value, entity.max_value, entity.step or None)
            ]
        case ESPhomeComponentType.SELECT, SelectInfo():
            options: dict[int, str] = dict(enumerate(entity.options))
            return [ParameterSpec("state", ParameterDataType.enum, control, valid_values=options)]
        case ESPhomeComponentType.LIGHT, LightInfo():
            # ColorMode values are bit masks of LightColorCapability, e.g. 35 = ON_OFF | BRIGHTNESS | RGB
            capabilities = 0
            for mode in entity.supported_color_modes:
                capabilities |= int(mode)
            specs = [ParameterSpec("state", boolean, control)]
            if capabilities & LightColorCapability.BRIGHTNESS:
                specs.append(ParameterSpec("brightness", decimal, control, PERCENT, 0, 100))
            if capabilities & LightColorCapability.RGB:
                specs += [
                    ParameterSpec("color_hue", decimal, control, ParameterUnit.arcdegree, 0, 360),
                    ParameterSpec("color_saturation", decimal, control, PERCENT, 0, 100),
                ]
            setting = ParameterVisibility.setting
            seconds = ParameterUnit.second
            specs += [
                # how long every later change of this light fades: kept here, added to each light command
                ParameterSpec("transition_length", decimal, control, seconds, 0, None, visibility=setting, local=True),
                # flash the light for a while: a command with one argument
                ParameterSpec(
                    "flash",
                    ParameterDataType.struct,
                    control,
                    visibility=setting,
                    fields=(ParameterSpec("length", decimal, control, seconds, 0, None),),
                ),
            ]
            return specs
        case ESPhomeComponentType.COVER, CoverInfo():
            return [
                ParameterSpec("position", decimal, control, PERCENT, 0, 100),
                ParameterSpec("operation", ParameterDataType.enum, sensor, valid_values=COVER_OPERATIONS),
            ]
        case ESPhomeComponentType.CLIMATE, ClimateInfo():
            celsius = ParameterUnit.celsius
            low, high = entity.visual_min_temperature, entity.visual_max_temperature
            step = entity.visual_target_temperature_step or None
            modes: dict[int, str] = {int(mode): mode.name for mode in entity.supported_modes}
            specs = [ParameterSpec("mode", ParameterDataType.enum, control, valid_values=modes)]
            if entity.supports_current_temperature:
                specs.append(ParameterSpec("current_temperature", decimal, sensor, celsius))
            targets = ("target_temperature_low", "target_temperature_high")
            if not entity.supports_two_point_target_temperature:
                targets = ("target_temperature",)
            return specs + [ParameterSpec(t, decimal, control, celsius, low, high, step) for t in targets]
    return []


def _number(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else float(value)


def _percent(value: float | None) -> float | None:
    """ESPHome levels are 0-1."""
    number = _number(value)
    return None if number is None else round(number * 100, 2)


def _hue_saturation(state: LightState) -> dict[str, float]:
    """Colour as the human model MajorDom uses (hue in degrees, saturation in percent), from ESPHome's RGB.

    Brightness is a parameter of its own, so only the colour is kept; black has no hue and reports nothing.
    """
    if state.red is None or state.green is None or state.blue is None:
        return {}
    hue, saturation, value = colorsys.rgb_to_hsv(state.red, state.green, state.blue)
    if value == 0:
        return {}
    return {"color_hue": round(hue * 360, 1), "color_saturation": round(saturation * 100, 1)}


def _index(value: IntEnum | None) -> int | None:
    return None if value is None else int(value)


def state_values(entity: EntityInfo, state: EntityState) -> dict[str, Any]:
    """Current values by sub-field, as plain Python values of the type the parameters declare."""
    values: dict[str, Any]
    match state:
        case _ if getattr(state, "missing_state", False):
            values = {}
        case LightState():
            values = {
                "state": bool(state.state),
                "brightness": _percent(state.brightness),
                **_hue_saturation(state),
            }
        case CoverState():
            values = {"position": _percent(state.position), "operation": _index(state.current_operation)}
        case ClimateState():
            values = {
                "mode": _index(state.mode),
                "current_temperature": _number(state.current_temperature),
                "target_temperature": _number(state.target_temperature),
                "target_temperature_low": _number(state.target_temperature_low),
                "target_temperature_high": _number(state.target_temperature_high),
            }
        case SelectState():
            options = entity.options if isinstance(entity, SelectInfo) else []
            values = {"state": options.index(state.state) if state.state in options else None}
        case SensorState() | NumberState():
            values = {"state": _number(state.state)}
        case TextSensorState():
            values = {"state": str(state.state)}
        case SwitchState() | BinarySensorState() | FanState():
            values = {"state": bool(state.state)}
        case _:
            values = {}
    component = _COMPONENT_BY_INFO.get(type(entity))
    if component in _HAND_WRITTEN:
        values.update(generic.state_values(component, state, HANDLED[component], entity))
    elif component:
        values = generic.state_values(component, state, entity=entity)
    return {sub_field: value for sub_field, value in values.items() if value is not None}


# Settings the integration keeps (nothing is sent to the device when they change), by (component, field)
LOCAL_SETTINGS = {("light", "transition_length"), ("lock", "code"), ("alarm_control_panel", "code")}
DEFAULT_FLASH_LENGTH = 2.0  # seconds, for a tap that sends no length


def is_local(parameter: ESPhomeParameter) -> bool:
    data = parameter.integration_data
    return (data.component_type, data.sub_field) in LOCAL_SETTINGS


def local_value(parameter: ESPhomeParameter, value: Any) -> Any:
    """A setting's value as it is kept, validated like a command value; an empty value clears it."""
    if value is None or value == "":
        return None
    if parameter.data_type == ParameterDataType.decimal:
        return _as_number(value, parameter)
    return str(value) if parameter.data_type == ParameterDataType.string else value


def build_command_args(
    parameter: ESPhomeParameter,
    value: Any,
    states: Mapping[int, EntityState],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keyword arguments of the `aioesphomeapi` `<component>_command` method that applies `value`.

    `settings` are the kept settings of the parameter's entity (field -> value), e.g. a light's transition.
    """
    if parameter.role != ParameterRole.control:
        raise ValueError(f"{parameter.name} is read-only")
    if is_local(parameter):
        raise ValueError(f"{parameter.name} is a setting: nothing is sent to the device")
    data = parameter.integration_data
    sub_field = data.sub_field
    assert data.service_key is not None
    args: dict[str, Any] = {"key": data.service_key}

    match data.component_type, sub_field:
        case ESPhomeComponentType.SWITCH | ESPhomeComponentType.FAN, "state":
            args["state"] = _as_bool(value)
        case ESPhomeComponentType.LIGHT, "state":
            args["state"] = _as_bool(value)
        case ESPhomeComponentType.LIGHT, "brightness":
            args["brightness"] = _as_number(value, parameter) / 100
        case ESPhomeComponentType.LIGHT, "flash":
            (length,) = generic.sub_parameters(parameter)
            given = value.get(str(length.id), value.get("length")) if isinstance(value, dict) else value
            args["state"] = True
            args["flash_length"] = DEFAULT_FLASH_LENGTH if given is None else _as_number(given, length)
        case ESPhomeComponentType.LIGHT, "color_hue" | "color_saturation":
            current = states.get(data.service_key)
            colour = _hue_saturation(current) if isinstance(current, LightState) else {}
            hue = colour.get("color_hue", 0.0)
            saturation = colour.get("color_saturation", 100.0)
            if sub_field == "color_hue":
                hue = _as_number(value, parameter)
            else:
                saturation = _as_number(value, parameter)
            args["rgb"] = colorsys.hsv_to_rgb(hue / 360, saturation / 100, 1.0)
        case ESPhomeComponentType.COVER, "position":
            args["position"] = _as_number(value, parameter) / 100
        case ESPhomeComponentType.NUMBER, "state":
            args["state"] = _as_number(value, parameter)
        case ESPhomeComponentType.SELECT, "state":
            options = parameter.valid_values or {}
            if value not in options:
                raise ValueError(f"{parameter.name}: no option {value!r}")
            args["state"] = options[value]
        case ESPhomeComponentType.BUTTON, "state":
            pass
        case ESPhomeComponentType.CLIMATE, "mode":
            if value not in (parameter.valid_values or {}):
                raise ValueError(f"{parameter.name}: unsupported mode {value!r}")
            args["mode"] = ClimateMode(value)
        case ESPhomeComponentType.CLIMATE, str() as target if target.startswith("target_temperature"):
            args[target] = _as_number(value, parameter)
        case _:  # a field the hand-written mapping does not cover: the generic one does
            args.update(generic.command_args(data.component_type, parameter, value, settings))
    transition = (settings or {}).get("transition_length")
    if data.component_type == ESPhomeComponentType.LIGHT and sub_field != "flash" and transition:
        args["transition_length"] = float(transition)
    return args
