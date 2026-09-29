from typing import Any

from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole

from .models import ESPhomeComponentType, ESPhomeParameterType

COMPONENT_TO_DATATYPE = {
    ESPhomeComponentType.LIGHT: ParameterDataType.string,
    ESPhomeComponentType.SWITCH: ParameterDataType.bool,
    ESPhomeComponentType.SENSOR: ParameterDataType.decimal,
    ESPhomeComponentType.BINARY_SENSOR: ParameterDataType.bool,
    ESPhomeComponentType.NUMBER: ParameterDataType.decimal,
    ESPhomeComponentType.SELECT: ParameterDataType.enum,
    ESPhomeComponentType.BUTTON: ParameterDataType.none,
    ESPhomeComponentType.COVER: ParameterDataType.enum,
    ESPhomeComponentType.FAN: ParameterDataType.enum,
    ESPhomeComponentType.CLIMATE: ParameterDataType.string,
    ESPhomeComponentType.TEXT_SENSOR: ParameterDataType.string,
}

COMPONENT_TO_ROLE = {
    ESPhomeComponentType.LIGHT: ParameterRole.control,
    ESPhomeComponentType.SWITCH: ParameterRole.control,
    ESPhomeComponentType.SENSOR: ParameterRole.sensor,
    ESPhomeComponentType.BINARY_SENSOR: ParameterRole.sensor,
    ESPhomeComponentType.NUMBER: ParameterRole.control,
    ESPhomeComponentType.SELECT: ParameterRole.control,
    ESPhomeComponentType.BUTTON: ParameterRole.control,
    ESPhomeComponentType.COVER: ParameterRole.control,
    ESPhomeComponentType.FAN: ParameterRole.control,
    ESPhomeComponentType.CLIMATE: ParameterRole.control,
    ESPhomeComponentType.TEXT_SENSOR: ParameterRole.sensor,
}

COMPONENT_TO_PARAMETER_TYPE = {
    ESPhomeComponentType.LIGHT: ESPhomeParameterType.STATE,
    ESPhomeComponentType.SWITCH: ESPhomeParameterType.STATE,
    ESPhomeComponentType.SENSOR: ESPhomeParameterType.SENSOR,
    ESPhomeComponentType.BINARY_SENSOR: ESPhomeParameterType.SENSOR,
    ESPhomeComponentType.NUMBER: ESPhomeParameterType.NUMBER,
    ESPhomeComponentType.SELECT: ESPhomeParameterType.SELECT,
    ESPhomeComponentType.BUTTON: ESPhomeParameterType.BUTTON,
    ESPhomeComponentType.COVER: ESPhomeParameterType.STATE,
    ESPhomeComponentType.FAN: ESPhomeParameterType.STATE,
    ESPhomeComponentType.CLIMATE: ESPhomeParameterType.STATE,
    ESPhomeComponentType.TEXT_SENSOR: ESPhomeParameterType.SENSOR,
}

COMPONENT_SUB_FIELDS: dict[ESPhomeComponentType, list[tuple[str, ParameterDataType, ParameterRole]]] = {
    ESPhomeComponentType.LIGHT: [
        ("state", ParameterDataType.bool, ParameterRole.control),
        ("brightness", ParameterDataType.decimal, ParameterRole.control),
        ("color_r", ParameterDataType.decimal, ParameterRole.control),
        ("color_g", ParameterDataType.decimal, ParameterRole.control),
        ("color_b", ParameterDataType.decimal, ParameterRole.control),
    ],
    ESPhomeComponentType.COVER: [
        ("position", ParameterDataType.decimal, ParameterRole.control),
        ("operation", ParameterDataType.string, ParameterRole.sensor),
    ],
    ESPhomeComponentType.CLIMATE: [
        ("mode", ParameterDataType.string, ParameterRole.control),
        ("current_temperature", ParameterDataType.decimal, ParameterRole.sensor),
        ("target_temperature", ParameterDataType.decimal, ParameterRole.control),
    ],
    ESPhomeComponentType.FAN: [
        ("state", ParameterDataType.bool, ParameterRole.control),
    ],
}


def get_sub_fields(component_type: ESPhomeComponentType) -> list[tuple[str, ParameterDataType, ParameterRole]]:
    return COMPONENT_SUB_FIELDS.get(component_type, [])


def convert_entity_state(state_obj: Any, component_type: str) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []

    if component_type == "switch":
        result.append(("state", state_obj.state))
    elif component_type == "light":
        result.append(("state", state_obj.state))
        if hasattr(state_obj, "brightness") and state_obj.brightness is not None:
            result.append(("brightness", state_obj.brightness))
        if hasattr(state_obj, "red") and state_obj.red is not None:
            result.append(("color_r", state_obj.red))
            result.append(("color_g", state_obj.green))
            result.append(("color_b", state_obj.blue))
    elif component_type == "cover":
        if hasattr(state_obj, "position") and state_obj.position is not None:
            result.append(("position", state_obj.position))
        if hasattr(state_obj, "current_operation") and state_obj.current_operation is not None:
            result.append(("operation", state_obj.current_operation))
    elif component_type in ("sensor", "binary_sensor", "number", "select", "text_sensor"):
        result.append(("state", state_obj.state))
    elif component_type == "climate":
        if hasattr(state_obj, "mode") and state_obj.mode is not None:
            result.append(("mode", state_obj.mode))
        if hasattr(state_obj, "current_temperature") and state_obj.current_temperature is not None:
            result.append(("current_temperature", state_obj.current_temperature))
        if hasattr(state_obj, "target_temperature") and state_obj.target_temperature is not None:
            result.append(("target_temperature", state_obj.target_temperature))
    elif component_type == "fan":
        result.append(("state", state_obj.state))
    else:
        result.append(("state", state_obj))

    return result


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
