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

# Kinds whose node legitimately has no parameters: the integration skips them on purpose
WITHOUT_PARAMETERS = generic.SKIPPED


def test_every_kind_the_library_knows_is_mapped_or_skipped_on_purpose():
    unknown = [
        kind
        for kind in catalogue.all_kinds()
        if kind not in mapper._HAND_WRITTEN and not generic.is_mapped(kind) and kind not in generic.SKIPPED
    ]

    assert unknown == [], "new entity kinds with no mapping: map them, or add them to generic.SKIPPED with a reason"


def test_every_kind_the_library_knows_has_a_recipe_or_a_reason():
    unexplained = [k for k in catalogue.all_kinds() if k not in catalogue.RECIPES and k not in catalogue.UNHOSTABLE]

    assert unexplained == [], "new entity kinds: add a recipe to catalogue.RECIPES, or a reason to catalogue.UNHOSTABLE"


def test_the_catalogue_tables_only_name_kinds_the_library_knows():
    stale = (set(catalogue.RECIPES) | set(catalogue.UNHOSTABLE)) - set(catalogue.all_kinds())

    assert stale == set(), f"kinds the library no longer knows: {sorted(stale)}"
    assert set(catalogue.RECIPES) & set(catalogue.UNHOSTABLE) == set()


def sample_value(parameter: ESPhomeParameter) -> Any:
    """A value the parameter accepts, picked from what the parameter itself declares."""
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
@pytest.mark.parametrize("kind", [k for k in catalogue.all_kinds() if k not in catalogue.UNHOSTABLE])
async def test_every_entity_kind_works_end_to_end(
    kind: str, controller: ESPhomeController, mdns: FakeMDNS, output, build_errors: dict[str, str | None]
):
    assert build_errors.get(kind) is None, build_errors.get(kind)
    sketch = catalogue.sketch_for(kind)
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
        if kind not in WITHOUT_PARAMETERS:
            assert device.parameters, f"a {kind} node produced no parameters"
        assert {p.integration_data.component_type for p in device.parameters} <= {kind}
        assert len({p.id for p in device.parameters}) == len(device.parameters)

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
