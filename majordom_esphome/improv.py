"""Wi-Fi setup of unprovisioned ESPHome nodes over Improv BLE, with `py-improv-ble-client` (the library HA uses).

A node built with `esp32_improv` (`improv_ble` from ESPHome 2026.11) and no Wi-Fi networks advertises the Improv
service; it is given the Hub's Wi-Fi over BLE, joins the LAN and then announces itself over mDNS like any other node.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from improv_ble_client import SERVICE_UUID, Error, ImprovBLEClient, State
from improv_ble_client import device_filter as waits_for_wifi
from improv_ble_client.errors import IMPROV_EXCEPTIONS, ImprovError, ProvisioningFailed

__all__ = ["SERVICE_UUID", "SetupError", "is_node", "provision", "waits_for_wifi"]

AUTHORIZATION_TIMEOUT = 60.0  # for the user to press the node's button
BLE_NAME_LENGTH = 20  # ESPHome cuts the name it advertises to this, keeping a `-xxxxxx` MAC suffix

PRESS_BUTTON = "Press the button on the device to let the Hub set it up"
NO_ANSWER = "The device did not answer over Bluetooth: move it closer to the Hub"


class SetupError(Exception):
    """The node could not be set up over BLE; the message is meant for the user."""


class _HideWiFiPassword(logging.Filter):
    """The library logs the Wi-Fi password at DEBUG, as text and hex-encoded in the frame it sends: drop both."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith(("%s: provision ssid", "TX: "))


logging.getLogger("improv_ble_client.client").addFilter(_HideWiFiPassword())


def is_node(ble_name: str, node_name: str) -> bool:
    """Whether the name a node advertises over BLE is its node name (the one it announces over mDNS)."""
    if len(node_name) <= BLE_NAME_LENGTH:
        return ble_name == node_name
    return ble_name in (node_name[:BLE_NAME_LENGTH], node_name[:13] + node_name[-7:])


async def provision(
    device: BLEDevice,
    advertisement: AdvertisementData,
    ssid: str,
    password: str,
    on_authorization_required: Callable[[], Awaitable[None]],
) -> None:
    """Give the node the Wi-Fi network; returns once it has joined it (ESPHome reports a failure after its
    `wifi_timeout`, and the library drops a silent node after 90 s). Raises `SetupError`."""
    client = ImprovBLEClient(device, advertisement)
    try:
        if await client.need_authorization():
            await on_authorization_required()
            await _wait_authorized(client)
        await client.provision(ssid, password, None)
    except ProvisioningFailed as exc:
        raise SetupError(_describe(exc.error, ssid)) from exc
    except (*IMPROV_EXCEPTIONS, ImprovError, BleakError) as exc:
        raise SetupError(NO_ANSWER) from exc


async def _wait_authorized(client: ImprovBLEClient) -> None:
    authorized = asyncio.Event()

    def on_state(state: State) -> None:
        if state == State.AUTHORIZED:
            authorized.set()

    unsubscribe = await client.subscribe_state_updates(on_state)
    try:
        async with asyncio.timeout(AUTHORIZATION_TIMEOUT):
            await authorized.wait()
    except TimeoutError:
        raise SetupError(f"{PRESS_BUTTON}, then pair it again") from None
    finally:
        unsubscribe()


def _describe(error: Error, ssid: str) -> str:
    if error == Error.UNABLE_TO_CONNECT:
        return f"The device could not join the Wi-Fi network {ssid}: check that it is a 2.4 GHz network in range"
    if error == Error.NOT_AUTHORIZED:
        return f"{PRESS_BUTTON}, then pair it again"
    return "The device could not be set up over Bluetooth"
