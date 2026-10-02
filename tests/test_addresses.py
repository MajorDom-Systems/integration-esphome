"""Where to reach a node: hostname first, then the announced IPs; what worked is remembered and refreshed by mDNS.

Only loopback addresses are used: `127.0.0.1` reaches the virtual device, `::1` is refused at once.
"""

import pytest
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import hub_creates_device, param, wait_for_value, wait_until
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import VirtualDevice


async def pair(controller: ESPhomeController, mdns: FakeMDNS, advert: Advert) -> ESPhomeDevice:
    await mdns.announce(advert)
    (discovery,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, discovery)
    await controller.pair_device(discovery, credentials=None)
    return await stored(controller, discovery.id)


async def stored(controller: ESPhomeController, device_id) -> ESPhomeDevice:
    async with controller.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
    assert device is not None
    return device


async def test_hostname_comes_first_then_the_announced_addresses(
    controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice
):
    paired = await pair(controller, mdns, Advert(port=device.port, server="localhost.", addresses=["::1", "127.0.0.1"]))

    assert paired.integration_data.addresses == ["localhost", "::1", "127.0.0.1"]


async def test_pairing_falls_back_to_the_next_address_and_remembers_what_worked(
    controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice
):
    paired = await pair(controller, mdns, Advert(port=device.port, server=None, addresses=["::1", "127.0.0.1"]))

    assert paired.integration_data.addresses == ["127.0.0.1", "::1"]  # the working one first, the failed one last


async def test_pairing_uses_the_latest_announcement(
    controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice
):
    await mdns.announce(Advert(port=device.port, server=None, addresses=["::1"]))  # the node's old address
    await mdns.announce(Advert(port=device.port, server=None, addresses=["127.0.0.1"]))  # DHCP moved it

    (discovery,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, discovery)
    await controller.pair_device(discovery, credentials=None)

    assert (await stored(controller, discovery.id)).integration_data.addresses == ["127.0.0.1"]


@pytest.mark.timeout(90)
async def test_address_changes_are_followed_while_paired(
    controller: ESPhomeController,
    mdns: FakeMDNS,
    device: VirtualDevice,
    output: RecordingControllerOutput,
):
    advert = Advert(port=device.port, server="localhost.", addresses=["127.0.0.1"])
    paired = await pair(controller, mdns, advert)
    assert paired.integration_data.addresses == ["localhost", "127.0.0.1"]

    # DHCP hands the node a new address: mDNS announces it, the old one is forgotten, the new one is tried first
    await mdns.announce(Advert(port=device.port, server="localhost.", addresses=["::1"]))
    assert (await stored(controller, paired.id)).integration_data.addresses == ["::1", "localhost"]

    # the next connection (the node reboots) tries `::1`, fails, and then remembers the hostname that worked
    output.connected_devices.clear()
    await device.restart()
    await wait_until(lambda: paired.id in output.connected_devices, 40, "the device to reconnect")
    await wait_for_value(output, param(paired, "Temperature"))
    assert (await stored(controller, paired.id)).integration_data.addresses == ["localhost", "::1"]


async def test_announcement_of_a_paired_node_without_changes_leaves_addresses_alone(
    controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice
):
    advert = Advert(port=device.port, server="localhost.", addresses=["127.0.0.1"])
    paired = await pair(controller, mdns, advert)

    await mdns.announce(advert)

    assert (await stored(controller, paired.id)).integration_data.addresses == paired.integration_data.addresses
