from typing import Any

from majordom_integration_sdk.schemas.parameter import ParameterUnit

SYSTEM_COMPONENTS = {
    "wifi",
    "mqtt",
    "api",
    "ota",
    "logger",
    "debug",
    "homeassistant",
    "web_server",
    "captive_portal",
    "mdns",
    "time",
    "esphome",
    "script",
    "automation",
    "template",
    "interval",
    "delay",
    "output",
    "gpio",
}


def _unit(name: str) -> ParameterUnit:
    return getattr(ParameterUnit, name, ParameterUnit.plain)


DEVICE_CLASS_UNITS = {
    "temperature": _unit("celsius"),
    "humidity": _unit("percentage"),
    "pressure": _unit("pascal"),
    "battery": _unit("percentage"),
    "illuminance": _unit("lux"),
    "power": _unit("watt"),
    "energy": _unit("joule"),
    "current": _unit("ampere"),
    "voltage": _unit("volt"),
    "frequency": _unit("hertz"),
    "gas": _unit("cubic_meter"),
    "water": _unit("liter"),
    "carbon_monoxide": _unit("ppm"),
    "carbon_dioxide": _unit("ppm"),
    "pm2_5": _unit("ppm"),
    "pm10": _unit("ppm"),
    "distance": _unit("meter"),
    "speed": _unit("mps"),
}


def get_unit(
    component_type: str, device_class: str | None = None, entity_config: dict[str, Any] | None = None
) -> ParameterUnit:
    if device_class and device_class in DEVICE_CLASS_UNITS:
        return DEVICE_CLASS_UNITS[device_class]
    if entity_config and entity_config.get("unit_of_measurement"):
        unit_str = entity_config["unit_of_measurement"].lower()
        for unit in ParameterUnit:
            if unit.value == unit_str:
                return unit
    return ParameterUnit.plain


def get_min_step(component_type: str, entity_config: dict[str, Any] | None = None) -> float | None:
    if entity_config and "step" in entity_config:
        return float(entity_config["step"])
    if component_type == "number":
        return 1.0
    if component_type == "sensor":
        return 0.1
    return None


def get_min_value(component_type: str, entity_config: dict[str, Any] | None = None) -> float | None:
    if entity_config and "min_value" in entity_config:
        return float(entity_config["min_value"])
    if component_type == "number":
        return 0.0
    return None


def get_max_value(component_type: str, entity_config: dict[str, Any] | None = None) -> float | None:
    if entity_config and "max_value" in entity_config:
        return float(entity_config["max_value"])
    if component_type == "number":
        return 100.0
    return None


def is_system_component(component_name: str) -> bool:
    return component_name.lower() in SYSTEM_COMPONENTS
