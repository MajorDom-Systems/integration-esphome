"""Enum parameters (a select, option lists, the library's integer enums): the value the Hub sends and the one reported.

An enum's value is the integer key of `valid_values`, its label the option or member name. The Hub may send either: the
key (also as a numeric string, as JSON may carry it) or the label. Both must reach the device as the same command, and
the device's state must come back as the key.
"""

from typing import Any

import pytest
from aioesphomeapi import (
    ClimateFanMode,
    ClimateInfo,
    ClimateMode,
    EntityInfo,
    FanInfo,
    LightInfo,
    SelectInfo,
    SelectState,
)

from majordom_esphome import mapper
from majordom_esphome.models import ESPhomeParameter
from tests.test_unhosted_fields import parameter_of, spec_of

# the library's dataclasses are not typed for construction
Select: Any = SelectInfo
Climate: Any = ClimateInfo
Fan: Any = FanInfo
Light: Any = LightInfo
Selected: Any = SelectState

MODE = Select(object_id="mode", key=9, name="Mode", options=["Eco", "Comfort", "Boost"])
THERMOSTAT = Climate(
    object_id="hvac",
    key=7,
    name="HVAC",
    supported_modes=[ClimateMode.OFF, ClimateMode.HEAT],
    supported_fan_modes=[ClimateFanMode.LOW, ClimateFanMode.HIGH],
    supported_custom_fan_modes=["Breeze", "Gust"],
)
FAN = Fan(object_id="fan", key=5, name="Fan", supported_preset_modes=["eco", "turbo"])
LIGHT = Light(object_id="light", key=3, name="Light", supported_color_modes=[1], effects=["None", "Rainbow"])


def parameter(entity: EntityInfo, sub_field: str) -> ESPhomeParameter:
    spec = spec_of(entity, sub_field)
    assert spec is not None
    return parameter_of(entity, spec)


def stored(parameter: ESPhomeParameter) -> ESPhomeParameter:
    """The parameter as it comes back from storage: JSON object keys are strings."""
    return ESPhomeParameter.model_validate_json(parameter.model_dump_json())


# (entity, sub_field, key, label, the argument the library's command gets)
CASES = [
    pytest.param(MODE, "state", 2, "Boost", "Boost", id="select"),
    pytest.param(THERMOSTAT, "mode", int(ClimateMode.HEAT), "HEAT", ClimateMode.HEAT, id="climate mode"),
    pytest.param(THERMOSTAT, "fan_mode", int(ClimateFanMode.HIGH), "HIGH", ClimateFanMode.HIGH, id="climate fan mode"),
    pytest.param(THERMOSTAT, "custom_fan_mode", 1, "Gust", "Gust", id="climate custom fan mode"),
    pytest.param(FAN, "preset_mode", 1, "turbo", "turbo", id="fan preset"),
    pytest.param(LIGHT, "effect", 1, "Rainbow", "Rainbow", id="light effect"),
]


@pytest.mark.parametrize(("entity", "sub_field", "key", "label", "sent"), CASES)
@pytest.mark.parametrize("given", ["key", "numeric string", "label"])
def test_an_enum_is_set_by_its_key_or_its_label(
    entity: EntityInfo, sub_field: str, key: int, label: str, sent: Any, given: str
):
    value = {"key": key, "numeric string": str(key), "label": label}[given]

    for target in (parameter(entity, sub_field), stored(parameter(entity, sub_field))):
        args = mapper.build_command_args(target, value, {})
        assert args == {"key": entity.key, sub_field: sent}
        assert type(args[sub_field]) is type(sent)


@pytest.mark.parametrize(("entity", "sub_field", "key", "label", "sent"), CASES)
@pytest.mark.parametrize("value", [99, "99", "no such option", True, None, 1.5])
def test_an_enum_value_that_names_no_option_is_rejected(
    entity: EntityInfo, sub_field: str, key: int, label: str, sent: Any, value: Any
):
    with pytest.raises(ValueError):
        mapper.build_command_args(parameter(entity, sub_field), value, {})


def test_a_select_state_is_reported_as_the_key_of_its_option():
    assert mapper.state_values(MODE, Selected(key=9, state="Boost")) == {"state": 2}
    assert mapper.state_values(MODE, Selected(key=9, state="Eco")) == {"state": 0}
    assert mapper.state_values(MODE, Selected(key=9, state="Unknown")) == {}  # not an option: nothing to report
