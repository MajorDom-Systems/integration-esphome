"""Controller lifecycle with a virtual ESPHome device: restarts, outages, shutdown."""

import asyncio

import pytest
from aioesphomeapi import APIClient
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import leaked_tasks, param, wait_for_value, wait_until
from tests.virtual.runner import VirtualDevice


async def stored_device(controller: ESPhomeController, device_id) -> ESPhomeDevice:
    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(device_id, as_=ESPhomeDevice)
    assert stored is not None
    return stored


async def flip_relay(device: VirtualDevice) -> None:
    client = APIClient(device.address, device.port, "")
    await client.connect(login=True)
    try:
        entities, _ = await client.list_entities_services()
        client.switch_command(next(e.key for e in entities if e.name == "Relay"), True)
        await asyncio.sleep(0.2)
    finally:
        await client.disconnect()


@pytest.mark.timeout(60)
async def test_paired_devices_reconnect_when_the_hub_restarts(
    controller: ESPhomeController, paired: ESPhomeDevice, output: RecordingControllerOutput
):
    await controller.stop()
    output.connected_devices.clear()
    output.events.clear()
    restarted = ESPhomeController(controller.dependencies)

    await restarted.start()
    try:
        await wait_until(lambda: paired.id in output.connected_devices, 15, "the paired device to reconnect")
        await wait_for_value(output, param(paired, "Temperature"))
        assert (await stored_device(restarted, paired.id)).available
    finally:
        await restarted.stop()


@pytest.mark.timeout(90)
async def test_outage_and_recovery_are_reported(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output: RecordingControllerOutput
):
    await wait_for_value(output, param(paired, "Temperature"))
    output.connected_devices.clear()

    await device.stop()
    await wait_until(lambda: paired.id in output.lost_devices, 15, "the outage to be reported")
    assert not (await stored_device(controller, paired.id)).available

    await device.start()
    await wait_until(lambda: paired.id in output.connected_devices, 40, "the recovery to be reported")
    recovered = await stored_device(controller, paired.id)
    assert recovered.available
    assert recovered.last_error is None
    output.events.clear()
    await wait_for_value(output, param(paired, "Temperature"))


@pytest.mark.timeout(90)
async def test_startup_with_an_offline_device_does_not_block_and_recovers(
    controller: ESPhomeController,
    paired: ESPhomeDevice,
    device: VirtualDevice,
    output: RecordingControllerOutput,
):
    await controller.stop()
    await device.stop()
    output.connected_devices.clear()
    restarted = ESPhomeController(controller.dependencies)

    async with asyncio.timeout(5):
        await restarted.start()  # a dead device must not hold the Hub's startup
    try:
        await wait_until(lambda: paired.id not in output.connected_devices, 1)
        await _wait_unavailable(restarted, paired)
        assert (await stored_device(restarted, paired.id)).last_error

        await device.start()
        await wait_until(lambda: paired.id in output.connected_devices, 40, "the device to connect once it is up")
        assert (await stored_device(restarted, paired.id)).last_error is None
    finally:
        await restarted.stop()


async def _wait_unavailable(controller: ESPhomeController, paired: ESPhomeDevice) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        stored = await stored_device(controller, paired.id)
        if not stored.available:
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("device never reported unavailable")


async def test_stop_closes_everything(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output: RecordingControllerOutput
):
    await wait_for_value(output, param(paired, "Temperature"))

    await controller.stop()
    output.events.clear()
    await flip_relay(device)
    await asyncio.sleep(1.5)

    assert output.events == []
    assert leaked_tasks() == []


async def test_unpair_stops_the_flow_of_events(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output: RecordingControllerOutput
):
    await wait_for_value(output, param(paired, "Temperature"))

    await controller.unpair(paired)
    output.events.clear()
    await flip_relay(device)
    await asyncio.sleep(1.5)

    assert output.events == []
    assert leaked_tasks() == []


async def test_identify_leaves_a_working_device(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output: RecordingControllerOutput
):
    await controller.identify(paired)  # ESPHome has no identify action: it must be a harmless no-op

    await flip_relay(device)
    await wait_for_value(output, param(paired, "Relay"), lambda v: v is True)
