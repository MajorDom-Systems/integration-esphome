"""Fields no virtual node can exercise, tested on library objects built directly (not end to end).

The host thermostat has no custom fan modes, so a climate with some cannot be hosted. The mapping of such a field is
still checked here: the parameter an entity description produces, the value a state maps to, and the command a value
becomes, with the same functions the controller calls.
"""

from typing import Any
from uuid import uuid4

import pytest
from aioesphomeapi import (
    ClimateInfo,
    ClimateMode,
    ClimateState,
    EntityInfo,
    LightInfo,
    LockCommand,
    LockInfo,
    UpdateInfo,
    WaterHeaterInfo,
    WaterHeaterState,
)
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)

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
        min_value=spec.min_value,
        max_value=spec.max_value,
        fields=[parameter_of(entity, child) for child in spec.fields] or None,
        integration_data=ESPhomeParameterIntegrationData(
            entity_name=entity.name,
            object_id=entity.object_id,
            component_type=mapper.component_type_of(entity) or "",
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


def test_a_lock_that_requires_a_code_offers_a_command_with_the_code_as_argument():
    lock: Any = LockInfo
    entity = lock(object_id="door", key=5, name="Door", requires_code=True)
    spec = spec_of(entity, "command_with_code")
    assert spec is not None
    assert spec.data_type == ParameterDataType.none
    assert [child.sub_field for child in spec.fields] == ["command", "code"]
    parameter = parameter_of(entity, spec)
    command, code = parameter.fields or []

    args = mapper.build_command_args(parameter, {str(command.id): 1, str(code.id): "1234"}, {})

    assert args == {"key": 5, "command": LockCommand.LOCK, "code": "1234"}
    with pytest.raises(ValueError):  # the code is part of the command: it is never sent without it
        mapper.build_command_args(parameter, {str(command.id): 1}, {})
    assert spec_of(lock(object_id="door", key=5, name="Door", requires_code=False), "command_with_code") is None


def test_water_heater_flags_are_switches_of_the_state_bit_mask():
    heater: Any = WaterHeaterInfo
    state: Any = WaterHeaterState
    entity = heater(object_id="boiler", key=9, name="Boiler", supported_features=8 | 16)  # away, on/off
    on, away = spec_of(entity, "on"), spec_of(entity, "away")
    assert on is not None and away is not None
    assert (on.data_type, on.role) == (ParameterDataType.bool, ParameterRole.control)

    assert {k: v for k, v in mapper.state_values(entity, state(key=9, state=2)).items() if k in ("on", "away")} == {
        "on": True,
        "away": False,
    }
    assert mapper.build_command_args(parameter_of(entity, on), False, {}) == {"key": 9, "on": False}
    assert spec_of(heater(object_id="boiler", key=9, name="Boiler", supported_features=0), "away") is None


def test_the_colour_brightness_is_only_offered_where_colour_and_white_mix():
    light: Any = LightInfo
    rgb_only = light(object_id="l", key=1, name="L", supported_color_modes=[35])  # RGB
    rgbw = light(object_id="l", key=1, name="L", supported_color_modes=[39])  # RGB + WHITE

    assert spec_of(rgb_only, "color_brightness") is None
    assert spec_of(rgbw, "color_brightness") is not None


def test_firmware_update_parameters_are_hidden():
    update: Any = UpdateInfo
    specs = mapper.parameter_specs(update(object_id="fw", key=3, name="Firmware"), "update")

    assert specs, "an update entity is still mapped"
    assert {spec.visibility for spec in specs} == {ParameterVisibility.system}


def test_water_heater_temperatures_are_in_celsius():
    heater: Any = WaterHeaterInfo
    entity = heater(object_id="boiler", key=9, name="Boiler", supported_features=1 | 2)  # current and target

    for field in ("current_temperature", "target_temperature"):
        spec = spec_of(entity, field)
        assert spec is not None and spec.unit == ParameterUnit.celsius, field
