"""Real hardware, end to end: an ESP32-C3 flashed with `esp32c3_improv.yaml` (no Wi-Fi, no API key) is found over BLE,
given the AP's Wi-Fi over Improv, found again over mDNS, given an API key over the zero PSK, and controlled.

Runs on the lab rig only, through `run_hardware.sh` (it brings the AP up, sets the Wi-Fi variables and passes
`--include-hardware-iot`); deselected elsewhere, like the Hub's hardware tests. Endpoints come from the environment:
ESPHOME_WIFI_SSID and ESPHOME_WIFI_PASSWORD (required: the AP the node joins), ESPHOME_BLE_ADAPTER (hci0),
ESPHOME_DISCOVERY_TIMEOUT (120 s for the node to advertise) and ESPHOME_TEST_TIMEOUT (300 s for the whole test).
"""

import asyncio
import functools
import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from aioesphomeapi import ZERO_NOISE_PSK, APIClient, APIConnectionError
from bleak import BleakScanner
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryService
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryService
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_dependencies

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import WiFi, hub_creates_device, param, wait_for_value, wait_until, with_wifi

NODE = "md-hw-improv"  # `esphome.name` of the sketch
BLE_ADAPTER = os.environ.get("ESPHOME_BLE_ADAPTER", "hci0")
DISCOVERY_TIMEOUT = float(os.environ.get("ESPHOME_DISCOVERY_TIMEOUT", "120"))

pytestmark = [pytest.mark.hardware_iot_device, pytest.mark.timeout(int(os.environ.get("ESPHOME_TEST_TIMEOUT", "300")))]


@pytest_asyncio.fixture
async def hub() -> AsyncIterator[ESPhomeController]:
    """The integration as the Hub runs it: real BLE and mDNS discovery, and the Hub's Wi-Fi."""
    ssid, password = os.environ.get("ESPHOME_WIFI_SSID"), os.environ.get("ESPHOME_WIFI_PASSWORD")
    assert ssid and password, "set ESPHOME_WIFI_SSID and ESPHOME_WIFI_PASSWORD (run_hardware.sh does)"
    zeroconf, ble = ZeroconfDiscoveryService(), BLEDiscoveryService()
    await zeroconf.start()
    # The SDK scans on BlueZ's default adapter; a scanned device keeps its adapter, so pairing connects through it too
    with patch(
        f"{BLEDiscoveryService.__module__}.BleakScanner",
        functools.partial(BleakScanner, bluez={"adapter": BLE_ADAPTER}),
    ):
        await ble.start()

    async def wifi_credentials() -> WiFi:
        return WiFi(ssid, password)

    sdk = build_test_dependencies().copy(zeroconf_discovery_service=zeroconf, ble_discovery_service=ble)
    controller = ESPhomeController(with_wifi(sdk, wifi_credentials))
    await controller.start()
    yield controller
    await controller.stop()
    await ble.stop()
    await zeroconf.stop()


async def connects(address: str, port: int, psk: str | None) -> bool:
    client = APIClient(address, port, "", noise_psk=psk)
    try:
        async with asyncio.timeout(10):
            await client.connect(login=True)
    except (APIConnectionError, TimeoutError):
        return False
    finally:
        await client.disconnect()
    return True


async def test_an_unprovisioned_node_is_set_up_over_ble_and_paired_with_a_key_of_its_own(hub: ESPhomeController):
    output = hub.dependencies.output
    assert isinstance(output, RecordingControllerOutput)

    def advertised() -> Discovery | None:
        return next((d for d in hub.discoveries.values() if d.transport == "ble" and d.device_name == NODE), None)

    await wait_until(lambda: advertised() is not None, DISCOVERY_TIMEOUT, f"{NODE} to advertise Improv")
    discovery = advertised()
    assert discovery is not None
    await hub_creates_device(hub.dependencies, discovery)

    device_id = await hub.pair_device(discovery, credentials=None)  # with an authorizer: press BOOT within 60 s

    assert device_id is not None
    async with hub.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
    assert device is not None and device.available
    data = device.integration_data
    assert data.encryption_key
    address = data.addresses[0]
    try:
        assert not await connects(address, data.port, None), "plaintext must be refused once the node has its key"
        assert not await connects(address, data.port, ZERO_NOISE_PSK), "so must the zero PSK"
        switch = param(device, "Test Switch")
        await hub.send_command(DeviceCommand(device_id=device_id, parameter_id=switch.id, value=True), device, switch)
        assert await wait_for_value(output, switch, lambda v: v is True, timeout=10)
    finally:  # back to an unprovisioned node (no Wi-Fi, no key) for the next run
        reset = param(device, "Factory Reset")
        await hub.send_command(DeviceCommand(device_id=device_id, parameter_id=reset.id, value=None), device, reset)
