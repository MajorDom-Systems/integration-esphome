"""Fields no virtual node can exercise, tested on library objects built directly (not end to end).

The host thermostat has no custom fan modes, so a climate with some cannot be hosted. The mapping of such a field is
still checked here: the parameter an entity description produces, the value a state maps to, and the command a value
becomes, with the same functions the controller calls.
"""

from typing import Any
from uuid import uuid4

import pytest
from aioesphomeapi import ClimateInfo, ClimateMode, ClimateState, EntityInfo
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole, ParameterVisibility

from majordom_esphome import mapper
from majordom_esphome.models import (
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ESPhomeParameterType,
    ParameterSpec,
)

# the library's dataclasses are not typed for construction
Info: Any = ClimateInfo
State: Any = ClimateState


def thermostat(**kwargs: Any) -> EntityInfo:
    return Info(
        object_id="probe",
        key=77,
        name="Probe",
        supported_modes=[ClimateMode.OFF, ClimateMode.HEAT],
        supports_two_point_target_temperature=False,
        **kwargs,
    )


def spec_of(entity: EntityInfo, sub_field: str) -> ParameterSpec | None:
    component = mapper.component_type_of(entity)
    assert component is not None
    return next((s for s in mapper.parameter_specs(entity, component) if s.sub_field == sub_field), None)


def parameter_of(entity: EntityInfo, spec: ParameterSpec) -> ESPhomeParameter:
    return ESPhomeParameter(
        id=uuid4(),
        name=spec.sub_field,
        data_type=spec.data_type,
        role=spec.role,
        visibility=spec.visibility or ParameterVisibility.user,
        valid_values=spec.valid_values,
        integration_data=ESPhomeParameterIntegrationData(
            entity_name=entity.name,
            object_id=entity.object_id,
            component_type="climate",
            parameter_type=ESPhomeParameterType.STATE,
            service_key=entity.key,
            sub_field=spec.sub_field,
        ),
    )


def test_custom_fan_modes_become_an_enum_of_the_offered_modes():
    spec = spec_of(thermostat(supported_custom_fan_modes=["Breeze", "Gust"]), "custom_fan_mode")

    assert spec is not None
    assert (spec.data_type, spec.role) == (ParameterDataType.enum, ParameterRole.control)
    assert spec.valid_values == {0: "Breeze", 1: "Gust"}
    assert spec.visibility == ParameterVisibility.setting


def test_no_custom_fan_modes_no_parameter():
    assert spec_of(thermostat(), "custom_fan_mode") is None


def test_a_custom_fan_mode_state_is_reported_as_the_index_of_the_option():
    entity = thermostat(supported_custom_fan_modes=["Breeze", "Gust"])

    assert mapper.state_values(entity, State(key=77, custom_fan_mode="Gust"))["custom_fan_mode"] == 1
    assert "custom_fan_mode" not in mapper.state_values(entity, State(key=77, custom_fan_mode=""))  # none set


def test_a_custom_fan_mode_command_sends_the_option_name():
    entity = thermostat(supported_custom_fan_modes=["Breeze", "Gust"])
    spec = spec_of(entity, "custom_fan_mode")
    assert spec is not None
    parameter = parameter_of(entity, spec)

    assert mapper.build_command_args(parameter, 1, {}) == {"key": 77, "custom_fan_mode": "Gust"}
    with pytest.raises(ValueError):
        mapper.build_command_args(parameter, 5, {})
