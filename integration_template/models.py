from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional, Any, List
from uuid import UUID

from majordom_hub.schemas.device import Device, Parameter, ParameterState
from majordom_hub.schemas.base import Base


class ESPhomeParameterType(str, Enum):
    STATE = "state"
    SENSOR = "sensor"
    NUMBER = "number"
    SELECT = "select"
    BUTTON = "button"


class ESPhomeComponentType(str, Enum):
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


class ESPhomeDeviceIntegrationData(Base):
    device_name: str | None = None
    unique_id: str | None = None
    address: str | None = None
    port: int = 6053
    encryption_key: str | None = None
    mac_address: str | None = None


class ESPhomeParameterIntegrationData(BaseModel):
    entity_name: str
    component_type: ESPhomeComponentType
    parameter_type: ESPhomeParameterType
    service_key: int | None = None
    sub_field: str | None = None  


class ESPhomeDevice(Device):
    integration_data: ESPhomeDeviceIntegrationData
    parameters: list["ESPhomeParameter"] = Field(default_factory=list) 


class ESPhomeParameter(Parameter):
    integration_data: ESPhomeParameterIntegrationData


class ESPhomeParameterState(ParameterState):
    integration_data: ESPhomeParameterIntegrationData
    value: Any = None