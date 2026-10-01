"""Translation between ESPHome entities/states/commands and MajorDom parameters/values/commands."""

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from aioesphomeapi import (
    COMPONENT_TYPE_TO_INFO,
    BinarySensorState,
    ClimateInfo,
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

from .esphome_spec import get_unit
from .models import ESPhomeComponentType, ESPhomeParameterType

# The library's own registry covers every entity ESPHome knows; only the types in ESPhomeComponentType are mapped.
_COMPONENT_BY_INFO: dict[type[EntityInfo], str] = {info: name for name, info in COMPONENT_TYPE_TO_INFO.items()}

PERCENT = ParameterUnit.percentage  # MajorDom expresses levels, positions and colour channels as 0-100


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


@dataclass(frozen=True)
class ParameterSpec:
    """What one MajorDom parameter of an entity looks like (everything except identity)."""

    sub_field: str
    data_type: ParameterDataType
    role: ParameterRole
    unit: ParameterUnit = ParameterUnit.plain
    min_value: float | None = None
    max_value: float | None = None
    min_step: float | None = None
    valid_values: dict[int, str] | None = None


def component_type_of(entity: EntityInfo) -> ESPhomeComponentType | None:
    """The mapped component type of an entity as reported by a device, or None if it is not supported (yet)."""
    try:
        return ESPhomeComponentType(_COMPONENT_BY_INFO.get(type(entity), ""))
    except ValueError:
        return None


def visibility_of(entity: EntityInfo) -> ParameterVisibility:
    if entity.disabled_by_default:
        return ParameterVisibility.system
    if entity.entity_category != EntityCategory.NONE:  # configuration and diagnostics
        return ParameterVisibility.setting
    return ParameterVisibility.user


def parameter_specs(entity: EntityInfo, component: ESPhomeComponentType) -> list[ParameterSpec]:
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
                specs += [ParameterSpec(f"color_{c}", decimal, control, PERCENT, 0, 100) for c in "rgb"]
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
                "color_r": _percent(state.red),
                "color_g": _percent(state.green),
                "color_b": _percent(state.blue),
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
    return {sub_field: value for sub_field, value in values.items() if value is not None}


def build_command_args(
    component_type: str,
    entity_key: int,
    sub_field: str | None,
    value: Any,
    current_state: dict[str, Any],
) -> dict:
    kwargs: dict[str, Any] = {"key": entity_key}

    if component_type == "switch":
        kwargs["state"] = bool(value)
    elif component_type == "light":
        if sub_field == "state":
            kwargs["state"] = bool(value)
        elif sub_field == "brightness":
            kwargs["brightness"] = float(value)
        elif sub_field in ("color_r", "color_g", "color_b"):
            r = value if sub_field == "color_r" else current_state.get("color_r", 0)
            g = value if sub_field == "color_g" else current_state.get("color_g", 0)
            b = value if sub_field == "color_b" else current_state.get("color_b", 0)
            kwargs["red"] = float(r) / 255.0
            kwargs["green"] = float(g) / 255.0
            kwargs["blue"] = float(b) / 255.0
        else:
            if isinstance(value, dict):
                if "state" in value:
                    kwargs["state"] = value["state"] == "ON"
                if "brightness" in value:
                    kwargs["brightness"] = value["brightness"]
                if "color" in value and isinstance(value["color"], dict):
                    kwargs["red"] = value["color"].get("r", 0) / 255.0
                    kwargs["green"] = value["color"].get("g", 0) / 255.0
                    kwargs["blue"] = value["color"].get("b", 0) / 255.0
            else:
                kwargs["state"] = bool(value)
    elif component_type == "cover":
        if sub_field == "position":
            kwargs["position"] = float(value)
        elif sub_field == "operation":
            kwargs["operation"] = str(value)
        elif isinstance(value, dict):
            kwargs.update(value)
        else:
            kwargs["position"] = 1.0 if value == "OPEN" else 0.0
    elif component_type == "number":
        kwargs["state"] = float(value)
    elif component_type == "select":
        kwargs["state"] = str(value)
    elif component_type == "button":
        pass
    elif component_type == "fan":
        if sub_field == "state":
            kwargs["state"] = bool(value)
        elif isinstance(value, bool):
            kwargs["state"] = value
        elif isinstance(value, dict):
            kwargs.update(value)
    elif component_type == "climate":
        if sub_field == "mode":
            kwargs["mode"] = str(value)
        elif sub_field == "target_temperature":
            kwargs["target_temperature"] = float(value)
        elif isinstance(value, dict):
            kwargs.update(value)

    return kwargs
