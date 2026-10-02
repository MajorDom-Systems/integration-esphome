"""The entity catalogue: every kind ESPHome's API knows is mapped, hosted by a virtual node, or explained.

Two layers, as in the Matter integration:

- fast checks over the installed `aioesphomeapi` (always run): a new kind that is neither mapped nor listed fails
  here, which is the signal to add support (or a recipe, or a reason);
- a sweep over one virtual node per kind (`pytest -m catalogue`, run by the monthly canary): pair it, read its
  states, send a command to every control parameter, and check that nothing breaks.
"""

import asyncio
from typing import Any

import pytest
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole

from majordom_esphome import generic, mapper
from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice, ESPhomeParameter
from tests.helpers import assert_values_match_parameters, hub_creates_device
from tests.virtual import catalogue
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import VirtualDevice


def test_every_kind_the_library_knows_is_mapped_or_skipped_on_purpose():
    unknown = [
        kind
        for kind in catalogue.all_kinds()
        if kind not in mapper._HAND_WRITTEN and not generic.is_mapped(kind) and kind not in generic.SKIPPED
    ]

    assert unknown == [], (
        f"new entity kinds with no mapping: map {unknown}, or add them to generic.SKIPPED with a reason"
    )


def test_every_kind_the_library_knows_has_a_recipe_or_a_reason():
    recipe_kinds = {catalogue.kind_of(key) for key in catalogue.RECIPES}
    unexplained = [k for k in catalogue.all_kinds() if k not in recipe_kinds and k not in catalogue.UNHOSTABLE]

    assert unexplained == [], (
        f"new entity kinds {unexplained}: write a recipe in catalogue.RECIPES (switch on every capability of the "
        "entity, so every field it exposes is tested), with its EXPECTED parameters and UNMAPPED fields, or explain in "
        "catalogue.UNHOSTABLE why the host node cannot run it"
    )


def test_the_catalogue_tables_agree():
    kinds = set(catalogue.all_kinds())
    named = {catalogue.kind_of(key) for key in (*catalogue.RECIPES, *catalogue.EXPECTED, *catalogue.UNMAPPED)}
    named |= set(catalogue.UNHOSTABLE)

    assert named - kinds == set(), f"kinds the library no longer knows: {sorted(named - kinds)}"
    assert {catalogue.kind_of(key) for key in catalogue.RECIPES} & set(catalogue.UNHOSTABLE) == set()
    assert set(catalogue.EXPECTED) == set(catalogue.RECIPES), "every recipe needs its EXPECTED parameters, and back"
    assert set(catalogue.UNMAPPED) <= set(catalogue.RECIPES)


def sample_value(parameter: ESPhomeParameter) -> Any:
    """A value the parameter accepts, picked from what the parameter itself declares."""
    if parameter.fields:  # a struct, or a command with arguments: a value per sub-parameter, keyed by its id
        subs = [ESPhomeParameter.model_validate(sub) for sub in parameter.fields]
        return {str(sub.id): sample_value(sub) for sub in subs}
    low, high = parameter.min_value, parameter.max_value
    match parameter.data_type:
        case ParameterDataType.bool:
            return True
        case ParameterDataType.none:
            return None
        case ParameterDataType.enum:
            return next(iter(parameter.valid_values or {0: ""}))
        case ParameterDataType.integer:
            return int(low) if low is not None else 1
        case ParameterDataType.decimal:
            return (low + high) / 2 if low is not None and high is not None else 1.0
        case _:
            return "x"


@pytest.mark.catalogue
@pytest.mark.timeout(120)
@pytest.mark.parametrize("recipe", catalogue.recipe_keys())
async def test_every_entity_kind_works_end_to_end(
    recipe: str, controller: ESPhomeController, mdns: FakeMDNS, output, build_errors: dict[str, str | None]
):
    kind = catalogue.kind_of(recipe)
    assert build_errors.get(recipe) is None, build_errors.get(recipe)
    sketch = catalogue.sketch_for(recipe)
    node = VirtualDevice(sketch)
    await node.start()
    try:
        await mdns.announce(Advert(name=sketch.name, mac=sketch.mac, port=sketch.port))
        (discovery,) = controller.discoveries.values()
        await hub_creates_device(controller.dependencies, discovery)

        await controller.pair_device(discovery, credentials=None)

        async with controller.dependencies.make_device_repository() as repo:
            device = await repo.get(discovery.id, as_=ESPhomeDevice)
        assert device is not None
        assert {p.integration_data.component_type for p in device.parameters} <= {kind}
        assert len({p.id for p in device.parameters}) == len(device.parameters)

        # every parameter is the one the recipe's entity must produce ...
        actual = {p.integration_data.sub_field: (p.data_type, p.role) for p in device.parameters}
        assert actual == catalogue.EXPECTED[recipe]

        # ... and every field of the entity's state and command is either exposed or explained
        state, arguments = generic.api_fields(kind)
        sub_fields = {
            ESPhomeParameter.model_validate(sub).integration_data.sub_field
            for p in device.parameters
            for sub in p.fields or []
        }
        unmapped = (state | arguments) - set(actual) - sub_fields  # a date's year is mapped as a field of `date`
        explained = set(catalogue.UNMAPPED.get(recipe, {}))
        assert unmapped == explained, (
            f"{kind}: fields neither mapped nor explained in UNMAPPED: {sorted(unmapped - explained)}; "
            f"explained but now mapped or gone: {sorted(explained - unmapped)}"
        )

        await asyncio.sleep(1.5)  # the node pushes its initial states
        assert_values_match_parameters(output, device)

        for parameter in device.parameters:
            if parameter.role != ParameterRole.control:
                continue
            command = DeviceCommand(device_id=device.id, parameter_id=parameter.id, value=sample_value(parameter))
            await controller.send_command(command, device, parameter)
            await asyncio.sleep(0.2)

        await asyncio.sleep(0.5)
        await controller.fetch(device)  # raises when the connection was lost
        assert node.running, f"the {kind} node died:\n{node.log[-1500:]}"
        assert_values_match_parameters(output, device)
    finally:
        await node.stop()
