"""Thread setup of unprovisioned ESPHome nodes over BLE, through the SDK's `BLERouter`: the Hub's active operational
dataset is written to the node, which joins the Hub's Thread network and then announces itself over mDNS (SRP, through
the border router) like any other node.

The node side is the `thread_provisioning` external component (ESPHome has no runtime dataset provisioning of its own):
an Improv-style GATT service, advertised while the node has no Thread network. Its frames are Improv's: command,
length, data, then a checksum (the sum of the previous bytes); a command is written in chunks that fit the connection.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from enum import IntEnum

from aioesphomeapi import APIConnectionError
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from majordom_integration_sdk.capabilities import BLEBusyError, BLEConnection, BLERouter

from .improv import BUSY, NO_ANSWER, PRESS_BUTTON, SetupError

__all__ = ["SERVICE_UUID", "provision", "waits_for_thread"]

SERVICE_UUID = "aeb52169-9cc0-4883-8ee7-e3d075fbe600"
STATE_UUID = "aeb52169-9cc0-4883-8ee7-e3d075fbe601"
ERROR_UUID = "aeb52169-9cc0-4883-8ee7-e3d075fbe602"
RPC_UUID = "aeb52169-9cc0-4883-8ee7-e3d075fbe603"
RESULT_UUID = "aeb52169-9cc0-4883-8ee7-e3d075fbe604"
SET_DATASET = 0x01

AUTHORIZATION_TIMEOUT = 60.0  # for the user to press the node's button
ATTACH_TIMEOUT = 90.0  # the node gives up after its own `attach_timeout` (60 s by default) and says so
LEASE_TIMEOUT = 30.0  # for a radio that hears the node to free up
ATT_HEADER = 3  # bytes of a write request that are not its value
OWNER = "esphome-thread"
NOTIFYING = (STATE_UUID, ERROR_UUID, RESULT_UUID)


class State(IntEnum):
    AWAITING_AUTHORIZATION = 0x01
    AUTHORIZED = 0x02
    ATTACHING = 0x03
    ATTACHED = 0x04


class Error(IntEnum):
    NONE = 0x00
    INVALID_RPC = 0x01
    UNKNOWN_RPC = 0x02
    UNABLE_TO_ATTACH = 0x03
    NOT_AUTHORIZED = 0x04
    INVALID_DATASET = 0x05
    UNKNOWN = 0xFF


def waits_for_thread(advertisement: AdvertisementData) -> bool:
    """The node advertises the service only while it has no Thread network."""
    return SERVICE_UUID in advertisement.service_uuids


def frame(command: int, data: bytes) -> bytes:
    body = bytes([command, len(data)]) + data
    return body + bytes([sum(body) & 0xFF])


async def provision(
    ble: BLERouter,
    address: str,
    dataset: bytes,
    on_authorization_required: Callable[[], Awaitable[None]],
):
    """Give the node the Thread network; returns once it has attached to it. The radio that hears the node best is held
    for the whole exchange. Raises `SetupError`."""
    try:
        async with ble.exclusive(address, owner=OWNER, timeout=LEASE_TIMEOUT) as lease:
            connection = await lease.connect()
            try:
                await _provision(connection, dataset, on_authorization_required)
            finally:
                with contextlib.suppress(Exception):
                    await connection.disconnect()
    except BLEBusyError as exc:
        raise SetupError(BUSY) from exc
    # BleakError and APIConnectionError: what a local adapter's and an ESPHome node's connections raise, until the SDK
    # normalises them
    except (ConnectionError, TimeoutError, LookupError, ValueError, BleakError, APIConnectionError) as exc:
        raise SetupError(NO_ANSWER) from exc


async def _provision(
    connection: BLEConnection, dataset: bytes, on_authorization_required: Callable[[], Awaitable[None]]
):
    (service,) = (s for s in await connection.services() if s.uuid.lower() == SERVICE_UUID)
    handles = {c.uuid.lower(): c.handle for c in service.characteristics}
    events: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
    for uuid in NOTIFYING:
        await connection.start_notify(handles[uuid], lambda data, uuid=uuid: events.put_nowait((uuid, data)))

    if State((await connection.read(handles[STATE_UUID]))[0]) == State.AWAITING_AUTHORIZATION:
        await on_authorization_required()
        authorized = (STATE_UUID, bytes([State.AUTHORIZED]))
        try:
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT):
                while await events.get() != authorized:
                    pass
        except TimeoutError:
            raise SetupError(f"{PRESS_BUTTON}, then pair it again") from None

    command = frame(SET_DATASET, dataset)
    size = connection.mtu - ATT_HEADER
    for start in range(0, len(command), size):
        await connection.write(handles[RPC_UUID], command[start : start + size], is_response_needed=True)
    async with asyncio.timeout(ATTACH_TIMEOUT):
        while True:
            uuid, data = await events.get()
            if uuid == ERROR_UUID and (error := Error(data[0])) != Error.NONE:
                raise SetupError(_describe(error))
            if uuid == STATE_UUID and data[0] == State.ATTACHED:
                return


def _describe(error: Error) -> str:
    if error == Error.UNABLE_TO_ATTACH:
        return "The device could not join the Hub's Thread network: check that the Hub's border router is in range"
    if error == Error.INVALID_DATASET:
        return "The device refused the Hub's Thread network settings"
    if error == Error.NOT_AUTHORIZED:
        return f"{PRESS_BUTTON}, then pair it again"
    return "The device could not be set up over Bluetooth"
