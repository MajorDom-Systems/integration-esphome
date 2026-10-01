"""ESPHome integration controller for MajorDom."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aioesphomeapi import EntityInfo, EntityState
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryInfo, ZeroconfDiscoveryService
from majordom_integration_sdk.schemas import (
    CredentialsType,
    DeviceParameterChange,
    ProvidedCredentials,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Discovery

from . import mapper
from .connection import ESPhomeDeviceConnection, describe_error
from .models import (
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
)

logger = logging.getLogger(__name__)


@dataclass
class _DiscoveryData:
    addresses: list[str]
    port: int
    mac: str | None
    node_name: str
    mdns_name: str


class ESPhomeController(AbstractController[ESPhomeDevice, ESPhomeParameter]):
    name = "esphome"

    def __init__(self, dependencies: AbstractController.Dependencies) -> None:
        super().__init__(dependencies)
        self._connections: dict[UUID, ESPhomeDeviceConnection] = {}
        self._discoveries: dict[UUID, Discovery] = {}
        self._discovery_data: dict[UUID, _DiscoveryData] = {}
        self._discovery_names: dict[str, UUID] = {}  # mDNS record name -> discovery id, for goodbyes
        self._zeroconf_cancel: Any | None = None
        self._paired: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._states: dict[UUID, dict[int, EntityState]] = {}

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self._discoveries.copy()

    @property
    def device_type(self) -> type[ESPhomeDevice]:
        return ESPhomeDevice

    @property
    def parameter_type(self) -> type[ESPhomeParameter]:
        return ESPhomeParameter

    async def start(self) -> None:
        logger.info("Starting ESPHome integration")

        self._zeroconf_cancel = self.dependencies.zeroconf_discovery_service.register(
            self, services={"_esphomelib._tcp.local."}
        )
        await self._load_paired_devices()

    async def stop(self) -> None:
        logger.info("Stopping ESPHome integration")
        if self._zeroconf_cancel:
            self._zeroconf_cancel()
            self._zeroconf_cancel = None

        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for connection in list(self._connections.values()):
            await connection.stop()
        self._connections.clear()
        self._states.clear()
        self._paired.clear()

    # ZeroconfDiscoveryListener: called by the SDK's discovery service

    async def zeroconf_did_discover_service(
        self, zeroconf: ZeroconfDiscoveryService, info: ZeroconfDiscoveryInfo
    ) -> None:
        await self._on_zeroconf_service(info)

    async def zeroconf_did_update_service(
        self, zeroconf: ZeroconfDiscoveryService, info: ZeroconfDiscoveryInfo
    ) -> None:
        await self._on_zeroconf_service(info)

    async def zeroconf_did_remove_service(self, zeroconf: ZeroconfDiscoveryService, type_: str, name: str) -> None:
        device_id = self._discovery_names.pop(name, None)
        if device_id is not None and self._discoveries.pop(device_id, None) is not None:
            self._discovery_data.pop(device_id, None)
            await self.dependencies.output.controller_did_lose_discovery(self, device_id)

    async def _on_zeroconf_service(self, service_info: ZeroconfDiscoveryInfo) -> None:
        try:
            node_name = service_info.name.removesuffix(f".{service_info.type_}")
            properties = service_info.decoded_properties
            mac = properties.get("mac")
            device_id = self.device_uuid(mac or node_name)
            addresses = self._announced_addresses(service_info, node_name)
            if device_id in self._paired:
                await self._refresh_addresses(device_id, addresses)
                return
            port = service_info.port or 6053
            encrypted = "api_encryption" in properties  # a key is configured (else `..._supported`)

            discovery = Discovery(
                id=device_id,
                integration=self.name,
                transport="tcp",
                device_manufacturer="esphome",
                device_name=properties.get("friendly_name") or node_name,
                device_category=None,  # a node may host anything
                device_icon=None,
                expected_credentials_options=[CredentialsType.secret if encrypted else CredentialsType.none],
            )
            self._discovery_data[device_id] = _DiscoveryData(addresses, port, mac, node_name, service_info.name)
            self._discovery_names[service_info.name] = device_id
            known = self._discoveries.get(device_id)
            if known == discovery:
                return
            self._discoveries[device_id] = discovery
            if known is None:
                await self.dependencies.output.controller_did_receive_discovery(self, discovery)
            else:
                await self.dependencies.output.controller_did_update_discovery(self, discovery)
            logger.debug("Discovered ESPHome node %s at %s:%s", node_name, addresses, port)
        except Exception:
            logger.exception("Error handling zeroconf discovery")

    @staticmethod
    def _announced_addresses(info: ZeroconfDiscoveryInfo, node_name: str) -> list[str]:
        """Hostname first (it follows the device through DHCP changes), then the announced IPs."""
        hostname = info.server.rstrip(".") if info.server else None
        addresses = [hostname, *(info.parsed_addresses or [])] if hostname else list(info.parsed_addresses or [])
        return list(dict.fromkeys(addresses)) or [f"{node_name}.local"]

    async def _refresh_addresses(self, device_id: UUID, announced: list[str]) -> None:
        """A paired device announced itself again: learn its new addresses, forget the ones it left."""
        device = await self._update_device(device_id)
        if device is None:
            return
        known = device.integration_data.addresses
        refreshed = [a for a in announced if a not in known] + [a for a in known if a in announced]
        if refreshed == known:
            return
        logger.info("Addresses of %s changed: %s -> %s", device_id, known, refreshed)
        await self._save_addresses(device_id, refreshed)
        if connection := self._connections.get(device_id):
            connection.addresses = refreshed

    async def _save_addresses(self, device_id: UUID, addresses: list[str]) -> None:
        async with self.dependencies.make_device_repository() as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is not None:
                data = device.integration_data.model_copy(update={"addresses": addresses})
                await repo.save(device.model_copy(update={"integration_data": data}))

    async def _load_paired_devices(self) -> None:
        async with self.dependencies.make_device_repository() as repo:
            devices = await repo.get_all(ESPhomeDevice)
        for device in devices:
            self._paired.add(device.id)
            self._connect(device)

    def _connect(self, device: ESPhomeDevice) -> None:
        data = device.integration_data
        if not data.addresses:
            logger.error("Device %s has no address, skipping", device.id)
            return
        connection = self._make_connection(device.id, data.addresses, data.port, data.encryption_key)
        self._connections[device.id] = connection
        self._spawn(connection.start())

    def _make_connection(
        self, device_id: UUID, addresses: list[str], port: int, encryption_key: str | None
    ) -> ESPhomeDeviceConnection:
        return ESPhomeDeviceConnection(
            device_id=device_id,
            addresses=addresses,
            port=port,
            encryption_key=encryption_key,
            on_state=self._on_state,
            on_availability=self._on_availability,
            on_addresses=self._on_addresses,
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run in the background, keeping a reference so the task is neither collected nor left behind on stop()."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_availability(self, device_id: UUID, available: bool, reason: str | None) -> None:
        if device_id not in self._connections:  # still pairing, or already unpaired
            return
        await self._update_device(device_id, available=available, last_error=None if available else reason)
        if available:
            await self.dependencies.output.controller_did_connect_device(self, device_id)
        else:
            await self.dependencies.output.controller_did_lose_device(self, device_id)

    async def _on_addresses(self, device_id: UUID, addresses: list[str]) -> None:
        if device_id in self._connections:  # not while pairing: the device is saved with the final order
            await self._save_addresses(device_id, addresses)

    async def _update_device(self, device_id: UUID, **changes: Any) -> ESPhomeDevice | None:
        async with self.dependencies.make_device_repository() as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is None:
                return None
            if any(getattr(device, field) != value for field, value in changes.items()):
                device = device.model_copy(update=changes)
                await repo.save(device)
            return device

    async def _on_state(self, device_id: UUID, entity: EntityInfo, state: EntityState) -> None:
        if device_id not in self._connections:  # still pairing, or already unpaired: the Hub does not know the device
            return
        try:
            self._states.setdefault(device_id, {})[state.key] = state
            events = self._events(device_id, entity, state)
            if events:
                await self.dependencies.output.controller_did_receive_events(self, events)
        except Exception:
            logger.exception("Error handling state for %s", entity.name)

    def _events(self, device_id: UUID, entity: EntityInfo, state: EntityState) -> list[DeviceParameterChange]:
        component = mapper.component_type_of(entity)
        if component is None:
            return []
        exposed = {spec.sub_field for spec in mapper.parameter_specs(entity, component)}
        return [
            DeviceParameterChange(
                device_id=device_id, parameter_id=self._parameter_id(device_id, entity, sub_field), value=value
            )
            for sub_field, value in mapper.state_values(entity, state).items()
            if sub_field in exposed
        ]

    def _parameter_id(self, device_id: UUID, entity: EntityInfo, sub_field: str) -> UUID:
        return self.parameter_uuid(device_id, f"{entity.object_id}_{sub_field}")

    async def _report_snapshot(self, device_id: UUID) -> None:
        """Report the last known value of every parameter in a single batch."""
        connection = self._connections[device_id]
        entities = connection.get_entities()
        events = [
            event
            for key, state in self._states.get(device_id, {}).items()
            if key in entities
            for event in self._events(device_id, entities[key], state)
        ]
        if events:
            await self.dependencies.output.controller_did_receive_events(self, events)

    async def pair_device(
        self,
        discovery: Discovery,
        credentials: ProvidedCredentials | None,
    ) -> None:
        logger.info("Pairing ESPHome device: %s", discovery.id)
        known = self._discoveries.get(discovery.id, discovery)
        conn: ESPhomeDeviceConnection | None = None
        try:
            integration_data = self._discovery_data.get(discovery.id)
            if integration_data is None:
                raise ValueError("Unknown device: it is no longer discovered")
            credentials_type = credentials.type if credentials else CredentialsType.none
            if credentials_type not in known.expected_credentials_options:
                raise ValueError(f"Unsupported credentials for this device: {credentials_type}")
            encryption_key = credentials.value if credentials and credentials_type == CredentialsType.secret else None
            if credentials_type == CredentialsType.secret and not encryption_key:
                raise ValueError("The encryption key is required for this device")

            conn = self._make_connection(
                discovery.id, integration_data.addresses, integration_data.port, encryption_key
            )
            await conn.start()
            await conn.wait_ready()

            parameters = [
                self._build_parameter(discovery.id, entity, component, spec)
                for entity in conn.get_entities().values()
                if (component := mapper.component_type_of(entity)) is not None
                for spec in mapper.parameter_specs(entity, component)
            ]
            main = next((p.id for p in parameters if p.can_be_main_parameter and p.role == "control"), None)

            async with self.dependencies.make_device_repository() as repo:
                hub_device = await repo.get(
                    discovery.id, as_=ESPhomeDevice
                )  # the Hub creates the device before pairing
            if hub_device is None:
                raise LookupError("The Hub has not created a device for this discovery")
            device = hub_device.model_copy(
                update={
                    "parameters": parameters,
                    "main_parameter": main,
                    "available": True,
                    "last_error": None,
                    "integration_data": ESPhomeDeviceIntegrationData(
                        device_name=discovery.device_name,
                        unique_id=integration_data.mac,
                        mac_address=integration_data.mac,
                        addresses=conn.addresses,
                        port=integration_data.port,
                        encryption_key=encryption_key,
                    ),
                }
            )

            async with self.dependencies.make_device_repository() as repo:
                await repo.save(device)

            self._connections[discovery.id] = conn
            self._paired.add(discovery.id)

            self._discoveries.pop(discovery.id, None)
            self._discovery_data.pop(discovery.id, None)
            await self.dependencies.output.controller_did_connect_device(self, discovery.id)
            await self._report_snapshot(discovery.id)

        except Exception as exc:
            if conn is not None:
                await conn.stop()
            logger.exception("Pairing %s failed", discovery.id)
            reason = str(exc) if isinstance(exc, ValueError | LookupError) else describe_error(exc.__cause__ or exc)
            failed = known.model_copy(update={"last_error": reason})
            self._discoveries[discovery.id] = failed
            await self.dependencies.output.controller_did_update_discovery(self, failed)
            raise

    def _build_parameter(
        self, device_id: UUID, entity: EntityInfo, component: Any, spec: mapper.ParameterSpec
    ) -> ESPhomeParameter:
        name = entity.name if spec.sub_field == "state" else f"{entity.name} {spec.sub_field.replace('_', ' ')}"
        return ESPhomeParameter(
            id=self._parameter_id(device_id, entity, spec.sub_field),
            name=name,
            data_type=spec.data_type,
            role=spec.role,
            unit=spec.unit,
            min_value=spec.min_value,
            max_value=spec.max_value,
            min_step=spec.min_step,
            valid_values=spec.valid_values,
            visibility=mapper.visibility_of(entity),
            integration_data=ESPhomeParameterIntegrationData(
                entity_name=entity.name,
                object_id=entity.object_id,
                component_type=component,
                parameter_type=mapper.PARAMETER_TYPE.get(component, mapper.ESPhomeParameterType.STATE),
                service_key=entity.key,
                sub_field=spec.sub_field,
            ),
        )

    async def unpair(self, device: ESPhomeDevice) -> None:
        connection = self._connections.pop(device.id, None)
        self._states.pop(device.id, None)
        self._paired.discard(device.id)
        if connection:
            await connection.stop()
        # The Hub removes the device record after unpair; the repository protocol
        # intentionally does not expose delete() to integrations.

    async def identify(self, device: ESPhomeDevice) -> None:
        # ESPHome has no identify action, so there is nothing to send to the device
        logger.info("Identify called for %s: not supported by ESPHome", device.id)

    async def fetch(self, device: ESPhomeDevice) -> None:
        if device.id not in self._connections:
            raise ConnectionError("Device not connected")
        await self._report_snapshot(device.id)

    async def send_command(
        self,
        command: DeviceCommand,
        device: ESPhomeDevice,
        parameter: ESPhomeParameter,
    ) -> None:
        connection = self._connections.get(device.id)
        if connection is None or not connection.ready:
            await self._update_device(device.id, last_error=f"{parameter.name} could not be set: device not connected")
            raise ConnectionError("Device not connected")

        args = mapper.build_command_args(parameter, command.value, self._states.get(device.id, {}))
        data = parameter.integration_data
        try:
            await connection.send_command(args["key"], data.component_type.value, args)
        except ConnectionError:
            await self._update_device(device.id, last_error=f"{parameter.name} could not be set: device not connected")
            raise
        await self._update_device(device.id, last_error=None)
