from dataclasses import dataclass
from enum import StrEnum

from majordom_integration_sdk.schemas.device import Device, Parameter
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)
from pydantic import BaseModel, Field


class ESPhomeParameterType(StrEnum):
    STATE = "state"
    SENSOR = "sensor"
    NUMBER = "number"
    SELECT = "select"
    BUTTON = "button"


class ESPhomeComponentType(StrEnum):
    LIGHT = "light"
    SWITCH = "switch"
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SELECT = "select"
    BUTTON = "button"
    COVER = "cover"
    FAN = "fan"
    CLIMATE = "climate"
    TEXT_SENSOR = "text_sensor"


class ESPhomeDeviceIntegrationData(BaseModel):
    device_name: str | None = None
    unique_id: str | None = None
    addresses: list[str] = Field(default_factory=list)  # hostname first, then IPs; ordered by past success
    port: int = 6053
    encryption_key: str | None = None
    mac_address: str | None = None


class ESPhomeParameterIntegrationData(BaseModel):
    entity_name: str
    object_id: str | None = None  # ESPHome's stable slug of the entity name; parameter ids derive from it
    component_type: str  # the library's name of the entity kind: an ESPhomeComponentType, or any other it reports
    parameter_type: ESPhomeParameterType
    service_key: int | None = None
    sub_field: str | None = None


class ESPhomeDevice(Device):
    integration_data: ESPhomeDeviceIntegrationData
    parameters: list["ESPhomeParameter"] = Field(default_factory=list)


class ESPhomeParameter(Parameter):
    integration_data: ESPhomeParameterIntegrationData


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
    visibility: ParameterVisibility | None = None  # None: from the entity
