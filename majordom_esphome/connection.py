import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aioesphomeapi import APIClient, EntityInfo, EntityState

logger = logging.getLogger(__name__)


class ESPhomeDeviceConnection:
    def __init__(
        self,
        device_id: Any,
        address: str,
        port: int,
        encryption_key: str | None,
        on_state: Callable[[Any, str, str, Any], Awaitable[None]],
    ):
        self.device_id = device_id
        self.address = address
        self.port = port
        self.encryption_key = encryption_key
        self.on_state_callback = on_state

        self._client: APIClient | None = None
        self._task: asyncio.Task | None = None
        self._entities: dict[int, EntityInfo] = {}
        self._ready = asyncio.Event()
        self._expected_disconnect = False
        self._stopped = False
        self._disconnect_event = asyncio.Event()
        self._disconnect_event.set()

    async def start(self):
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
                self._ready.clear()
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

        entities = await self._client.list_entities_services()
        self._entities.clear()
        for ent in entities:
            if isinstance(ent, EntityInfo) and hasattr(ent, "key") and hasattr(ent, "name"):
                self._entities[ent.key] = ent

        self._ready.set()

        def state_callback(state: EntityState):
            asyncio.create_task(self._handle_state(state))

        client = self._client
        if client is not None:
            await client.subscribe_states(state_callback)  # type: ignore
        else:
            logger.error("Client is None, cannot subscribe to states")

        await self._disconnect_event.wait()

    async def _handle_state(self, state: EntityState):
        entity = self._entities.get(state.key)
        if not entity:
            return
        component_type = getattr(entity, "type", "unknown")
        await self.on_state_callback(self.device_id, entity.name, component_type, state)

    async def wait_ready(self):
        await self._ready.wait()

    def get_entities(self) -> dict[int, EntityInfo]:
        return self._entities.copy()

    async def send_command(self, entity_key: int, component_type: str, command_args: dict):
        if not self._client:
            raise ConnectionError("Device not connected")
        method_name = f"{component_type}_command"
        method = getattr(self._client, method_name, None)
        if not method:
            raise ValueError(f"Unknown component type: {component_type}")
        await method(**command_args)
