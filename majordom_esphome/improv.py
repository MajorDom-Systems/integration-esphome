"""Wi-Fi setup of unprovisioned ESPHome nodes over Improv BLE, through the SDK's `BLERouter`, so a node is reached
by whichever radio hears it best: the Hub's own adapter or another ESPHome node lending its Bluetooth.
`py-improv-ble-client` (the library HA uses) supplies only the protocol: the advert filter and the frames.

A node built with `esp32_improv` (`improv_ble` from ESPHome 2026.11) and no Wi-Fi networks advertises the Improv
service; it is given the Hub's Wi-Fi over BLE, joins the LAN and then announces itself over mDNS like any other node.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable

from aioesphomeapi import APIConnectionError
from bleak.exc import BleakError
from improv_ble_client import SERVICE_UUID, Error, State
from improv_ble_client import device_filter as waits_for_wifi
from improv_ble_client.errors import ImprovError
from improv_ble_client.protocol import (
    CHARACTERISTIC_UUID_ERROR,
    CHARACTERISTIC_UUID_RPC_COMMAND,
    CHARACTERISTIC_UUID_RPC_RESULT,
    CHARACTERISTIC_UUID_STATE,
    WiFiSettingsCmd,
    WiFiSettingsRes,
    parse_result,
)
from majordom_integration_sdk.capabilities import (
    BLEBusyError,
    BLEConnection,
    BLEPairingError,
    BLERouter,
    LESecureConnections,
    SecureChannel,
)

__all__ = [
    "SERVICE_UUID",
    "EncryptionError",
    "SetupError",
    "is_node",
    "provision",
    "secured_connection",
    "waits_for_wifi",
]

AUTHORIZATION_TIMEOUT = 60.0  # for the user to press the node's button
PROVISION_TIMEOUT = 90.0  # from giving the node the Wi-Fi to its answer
LEASE_TIMEOUT = 30.0  # for a radio that hears the node to free up
BLE_NAME_LENGTH = 20  # ESPHome cuts the name it advertises to this, keeping a `-xxxxxx` MAC suffix
OWNER = "esphome-improv"
NOTIFYING = (CHARACTERISTIC_UUID_STATE, CHARACTERISTIC_UUID_ERROR, CHARACTERISTIC_UUID_RPC_RESULT)

PRESS_BUTTON = "Press the button on the device to let the Hub set it up"
NO_ANSWER = "The device did not answer over Bluetooth: move it closer to the Hub"
BUSY = "The Hub's Bluetooth is busy with another device: try again in a minute"
NOT_ENCRYPTED = (
    "The Hub could not set up an encrypted Bluetooth link to the device, so it sent it nothing: move it closer to the "
    "Hub and try again"
)


class SetupError(Exception):
    """The node could not be set up over BLE; the message is meant for the user."""


class EncryptionError(SetupError):
    """The link could not be secured, so no credentials were sent (fail closed)."""


@contextlib.asynccontextmanager
async def secured_connection(
    ble: BLERouter, address: str, owner: str, lease_timeout: float, channel: SecureChannel | None = None
) -> AsyncGenerator[BLEConnection]:
    """The node's connection, secured by ``channel`` (LE Secure Connections by default) before anything is written,
    through the radio that hears it best (of those that can pair, if the channel pairs), held for the whole exchange.
    Bluetooth failures become `SetupError`s."""
    channel = channel or LESecureConnections()
    try:
        async with (
            ble.exclusive(
                address, owner=owner, busy_timeout=lease_timeout, is_pairing_needed=channel.is_pairing_needed
            ) as lease,
            lease.connect() as connection,
            channel.secure(connection) as secured,
        ):
            yield secured
    except BLEPairingError as exc:
        raise EncryptionError(NOT_ENCRYPTED) from exc
    except BLEBusyError as exc:
        raise SetupError(BUSY) from exc
    # BleakError and APIConnectionError: what a local adapter's and an ESPHome node's connections raise, until the SDK
    # normalises them
    except (ConnectionError, TimeoutError, LookupError, ValueError, ImprovError, BleakError, APIConnectionError) as exc:
        raise SetupError(NO_ANSWER) from exc


def is_node(ble_name: str, node_name: str) -> bool:
    """Whether the name a node advertises over BLE is its node name (the one it announces over mDNS)."""
    if len(node_name) <= BLE_NAME_LENGTH:
        return ble_name == node_name
    return ble_name in (node_name[:BLE_NAME_LENGTH], node_name[:13] + node_name[-7:])


async def provision(
    ble: BLERouter,
    address: str,
    ssid: str,
    password: str,
    on_authorization_required: Callable[[], Awaitable[None]],
    channel: SecureChannel | None = None,
):
    """Give the node the Wi-Fi network; returns once it has joined it (ESPHome reports a failure after its
    `wifi_timeout`). Raises `SetupError`, `EncryptionError` when the link could not be secured."""
    async with secured_connection(ble, address, OWNER, LEASE_TIMEOUT, channel) as connection:
        await _provision(connection, ssid, password, on_authorization_required)


async def _provision(
    connection: BLEConnection, ssid: str, password: str, on_authorization_required: Callable[[], Awaitable[None]]
):
    (service,) = (s for s in await connection.services() if s.uuid.lower() == SERVICE_UUID)
    handles = {c.uuid.lower(): c.handle for c in service.characteristics}
    events: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
    for uuid in NOTIFYING:
        await connection.start_notify(handles[uuid], lambda data, uuid=uuid: events.put_nowait((uuid, data)))

    if State((await connection.read(handles[CHARACTERISTIC_UUID_STATE]))[0]) == State.AUTHORIZATION_REQUIRED:
        await on_authorization_required()
        authorized = (CHARACTERISTIC_UUID_STATE, bytes([State.AUTHORIZED]))
        try:
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT):
                while await events.get() != authorized:
                    pass
        except TimeoutError:
            raise SetupError(f"{PRESS_BUTTON}, then pair it again") from None

    command = WiFiSettingsCmd(ssid.encode(), password.encode()).as_bytes()
    await connection.write(handles[CHARACTERISTIC_UUID_RPC_COMMAND], command, is_response_needed=True)
    async with asyncio.timeout(PROVISION_TIMEOUT):
        while True:
            uuid, data = await events.get()
            if uuid == CHARACTERISTIC_UUID_ERROR and (error := Error(data[0])) != Error.NO_ERROR:
                raise SetupError(_describe(error, ssid))
            if uuid == CHARACTERISTIC_UUID_RPC_RESULT and isinstance(parse_result(data), WiFiSettingsRes):
                return


def _describe(error: Error, ssid: str) -> str:
    if error == Error.UNABLE_TO_CONNECT:
        return f"The device could not join the Wi-Fi network {ssid}: check that it is a 2.4 GHz network in range"
    if error == Error.NOT_AUTHORIZED:
        return f"{PRESS_BUTTON}, then pair it again"
    return "The device could not be set up over Bluetooth"
