from majordom_integration_sdk.schemas.parameter import ParameterUnit

# ESPHome reports a unit string per entity; it is exact, so it wins over the device class.
UNITS_BY_SYMBOL: dict[str, ParameterUnit] = {
    "°c": ParameterUnit.celsius,
    "k": ParameterUnit.kelvin,
    "%": ParameterUnit.percentage,
    "pa": ParameterUnit.pascal,
    "lx": ParameterUnit.lux,
    "w": ParameterUnit.watt,
    "kwh": ParameterUnit.kwh,
    "j": ParameterUnit.joule,
    "a": ParameterUnit.ampere,
    "v": ParameterUnit.volt,
    "hz": ParameterUnit.hertz,
    "ppm": ParameterUnit.ppm,
    "µg/m³": ParameterUnit.ugm3,
    "μg/m³": ParameterUnit.ugm3,
    "ug/m3": ParameterUnit.ugm3,
    "m": ParameterUnit.meters,
    "m/s": ParameterUnit.mps,
    "s": ParameterUnit.second,
    "kg": ParameterUnit.kilogram,
    "rpm": ParameterUnit.rpm,
}

# Used only when the entity declares no unit of its own.
UNITS_BY_DEVICE_CLASS: dict[str, ParameterUnit] = {
    "temperature": ParameterUnit.celsius,
    "humidity": ParameterUnit.percentage,
    "moisture": ParameterUnit.percentage,
    "battery": ParameterUnit.percentage,
    "illuminance": ParameterUnit.lux,
    "power": ParameterUnit.watt,
    "voltage": ParameterUnit.volt,
    "current": ParameterUnit.ampere,
    "frequency": ParameterUnit.hertz,
    "pm25": ParameterUnit.ugm3,
    "pm10": ParameterUnit.ugm3,
    "carbon_dioxide": ParameterUnit.ppm,
    "carbon_monoxide": ParameterUnit.ppm,
}


def get_unit(device_class: str | None = None, unit_of_measurement: str | None = None) -> ParameterUnit:
    if unit_of_measurement:
        return UNITS_BY_SYMBOL.get(unit_of_measurement.lower(), ParameterUnit.plain)
    return UNITS_BY_DEVICE_CLASS.get(device_class or "", ParameterUnit.plain)
