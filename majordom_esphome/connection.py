import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    EntityInfo,
    EntityState,
    InvalidEncryptionKeyAPIError,
    RequiresEncryptionAPIError,
)

logger = logging.getLogger(__name__)

ADDRESS_TIMEOUT = 3.0  # seconds, per address: a dead address must not delay the next one
WAIT_READY_TIMEOUT = 30.0
RECONNECT_DELAY_FIRST = 1.0
RECONNECT_DELAY_MAX = 30.0

StateCallback = Callable[[UUID, EntityInfo, EntityState], Awaitable[None]]
AvailabilityCallback = Callable[[UUID, bool, str | None], Awaitable[None]]  # (device id, available, reason if not)
AddressesCallback = Callable[[UUID, list[str]], Awaitable[None]]


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
    """One device's native-API session: connects, keeps reconnecting with backoff, and feeds states to a callback."""

    def __init__(
        self,
        device_id: UUID,
        addresses: list[str],
        port: int,
        encryption_key: str | None,
        on_state: StateCallback,
        on_availability: AvailabilityCallback | None = None,
        on_addresses: AddressesCallback | None = None,
    ) -> None:
        self.device_id = device_id
        self.addresses = list(addresses)  # hostname and IPs, tried in order; reordered by what worked
        self.port = port
        self.encryption_key = encryption_key
        self.on_state_callback = on_state
        self.on_availability_callback = on_availability
        self.on_addresses_callback = on_addresses

        self.last_error: Exception | None = None
        self._client: APIClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._entities: dict[int, EntityInfo] = {}
        self._states: asyncio.Queue[EntityState] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._available: bool | None = None  # what the callback was last told

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def start(self) -> None:
        self._failed.clear()
        self.last_error = None
        self._consumer = asyncio.create_task(self._consume_states())
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for task in (self._task, self._consumer):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._consumer = None
        await self._close_client()
        self._ready.clear()

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

    async def send_command(self, entity_key: int, component_type: str, command_args: dict) -> None:
        if self._client is None or not self.ready:
            raise ConnectionError("Device not connected")
        method = getattr(self._client, f"{component_type}_command", None)
        if method is None:
            raise ValueError(f"Unknown component type: {component_type}")
        try:
            method(**command_args)  # fire-and-forget: the device answers with a state update
        except APIConnectionError as exc:
            raise ConnectionError("Device not connected") from exc

    async def _run(self) -> None:
        delay = RECONNECT_DELAY_FIRST
        while True:
            try:
                await self._connect()
                delay = RECONNECT_DELAY_FIRST
                await self._disconnected.wait()
                raise ConnectionError("connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Connection to %s (%s, port %s) failed: %r", self.device_id, self.addresses, self.port, exc
                )
                self.last_error = exc
                self._ready.clear()
                self._failed.set()
                await self._close_client()
                await self._report_availability(False, describe_error(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _connect(self) -> None:
        """Connect through the first address that works; remember what worked for the next connection."""
        self._disconnected.clear()
        failed: list[str] = []
        error: Exception = ConnectionError("The device has no address")
        for address in list(self.addresses):
            try:
                async with asyncio.timeout(ADDRESS_TIMEOUT):
                    await self._connect_to(address)
            except Exception as exc:
                logger.info("Device %s is not reachable at %s: %r", self.device_id, address, exc)
                error = exc
                failed.append(address)
                await self._close_client()
                continue
            await self._remember(address, failed)
            return
        raise error

    async def _connect_to(self, address: str) -> None:
        client = APIClient(address, self.port, None, noise_psk=self.encryption_key)
        self._client = client
        await client.connect(login=True, on_stop=self._on_stop)
        entities, _services = await client.list_entities_services()
        self._entities = {entity.key: entity for entity in entities}
        client.subscribe_states(self._state_received)
        self.last_error = None
        self._ready.set()
        logger.info("Connected to ESPHome device %s at %s:%s", self.device_id, address, self.port)

    async def _remember(self, address: str, failed: list[str]) -> None:
        """Working address first, addresses that failed this time last."""
        untried = [a for a in self.addresses if a != address and a not in failed]
        remembered = [address, *untried, *failed]
        if remembered != self.addresses:
            self.addresses = remembered
            if self.on_addresses_callback is not None:
                try:
                    await self.on_addresses_callback(self.device_id, remembered)
                except Exception:
                    logger.exception("Error saving the addresses of %s", self.device_id)
        await self._report_availability(True, None)

    async def _on_stop(self, expected_disconnect: bool) -> None:
        logger.info("Device %s disconnected (expected=%s)", self.device_id, expected_disconnect)
        self._ready.clear()
        self._disconnected.set()

    def _state_received(self, state: EntityState) -> None:
        self._states.put_nowait(state)

    async def _consume_states(self) -> None:
        """Deliver states one at a time, in the order the device sent them."""
        while True:
            state = await self._states.get()
            entity = self._entities.get(state.key)
            if entity is None:
                continue
            try:
                await self.on_state_callback(self.device_id, entity, state)
            except Exception:
                logger.exception("Error handling state of %s on %s", entity.name, self.device_id)

    async def _report_availability(self, available: bool, reason: str | None) -> None:
        if self.on_availability_callback is None or self._available == available:
            return
        self._available = available
        try:
            await self.on_availability_callback(self.device_id, available, reason)
        except Exception:
            logger.exception("Error reporting availability of %s", self.device_id)

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
