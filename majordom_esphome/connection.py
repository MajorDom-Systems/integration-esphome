import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    EntityInfo,
    EntityState,
    InvalidEncryptionKeyAPIError,
    RequiresEncryptionAPIError,
)

logger = logging.getLogger(__name__)

WAIT_READY_TIMEOUT = 30.0


def describe_error(exc: BaseException) -> str:
    """Plain-language reason for the user; the technical detail stays in the logs."""
    if isinstance(exc, RequiresEncryptionAPIError):
        return "The device requires an encryption key"
    if isinstance(exc, InvalidEncryptionKeyAPIError):
        return "The device rejected the encryption key"
    if isinstance(exc, TimeoutError):
        return "The device did not respond in time"
    return "The device is not reachable"


class ESPhomeDeviceConnection:
    def __init__(
        self,
        device_id: Any,
        address: str,
        port: int,
        encryption_key: str | None,
        on_state: Callable[[Any, EntityInfo, EntityState], Awaitable[None]],
    ):
        self.device_id = device_id
        self.address = address
        self.port = port
        self.encryption_key = encryption_key
        self.on_state_callback = on_state

        self._client: APIClient | None = None
        self._task: asyncio.Task | None = None
        self._entities: dict[int, EntityInfo] = {}
        self.last_error: Exception | None = None
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._expected_disconnect = False
        self._stopped = False
        self._disconnect_event = asyncio.Event()
        self._disconnect_event.set()

    async def start(self):
        self._failed.clear()
        self.last_error = None
        self._stopped = False
        self._expected_disconnect = False
        self._disconnect_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stopped = True
        self._expected_disconnect = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client:
            await self._client.disconnect()

    async def _on_stop(self, expected_disconnect: bool):
        logger.info(
            "Device %s disconnected (expected=%s)",
            self.device_id,
            expected_disconnect,
        )
        self._ready.clear()
        self._disconnect_event.set()
        if not expected_disconnect and not self._expected_disconnect:
            logger.warning("Unexpected disconnect from %s", self.device_id)

    async def _run(self):
        while not self._stopped:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Connection to %s lost: %s, reconnecting in 5s",
                    self.address,
                    e,
                )
                self.last_error = e
                self._ready.clear()
                self._failed.set()
                await asyncio.sleep(5)

    async def _connect_and_listen(self):
        self._client = APIClient(
            address=self.address,
            port=self.port,
            noise_psk=self.encryption_key,
            password=None,
        )
        await self._client.connect(login=True, on_stop=self._on_stop)
        logger.info(
            "Connected to ESPHome device %s at %s:%s",
            self.device_id,
            self.address,
            self.port,
        )

        entities, _services = await self._client.list_entities_services()
        self._entities.clear()
        for ent in entities:
            if isinstance(ent, EntityInfo) and hasattr(ent, "key") and hasattr(ent, "name"):
                self._entities[ent.key] = ent

        self._ready.set()

        def state_callback(state: EntityState):
            asyncio.create_task(self._handle_state(state))

        client = self._client
        if client is not None:
            client.subscribe_states(state_callback)  # synchronous: awaiting None raised and dropped the connection
        else:
            logger.error("Client is None, cannot subscribe to states")

        await self._disconnect_event.wait()

    async def _handle_state(self, state: EntityState):
        entity = self._entities.get(state.key)
        if not entity:
            return
        await self.on_state_callback(self.device_id, entity, state)

    async def wait_ready(self, timeout: float = WAIT_READY_TIMEOUT) -> None:
        """Wait for the first successful connection; fail as soon as an attempt fails (no endless retrying)."""
        waiters = {asyncio.create_task(self._ready.wait()), asyncio.create_task(self._failed.wait())}
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._ready.is_set():
            return
        if self.last_error is not None:
            raise ConnectionError(describe_error(self.last_error)) from self.last_error
        raise TimeoutError("The device did not respond in time")

    def get_entities(self) -> dict[int, EntityInfo]:
        return self._entities.copy()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def send_command(self, entity_key: int, component_type: str, command_args: dict):
        if self._client is None or not self.ready:
            raise ConnectionError("Device not connected")
        method = getattr(self._client, f"{component_type}_command", None)
        if method is None:
            raise ValueError(f"Unknown component type: {component_type}")
        try:
            method(**command_args)  # fire-and-forget: the device answers with a state update
        except APIConnectionError as exc:
            raise ConnectionError("Device not connected") from exc
