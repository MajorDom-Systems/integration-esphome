"""ESPHome integration controller for MajorDom."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from aioesphomeapi import ZERO_NOISE_PSK, APIClient, EntityInfo, EntityState
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from majordom_integration_sdk import Need
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryInfo, BLEDiscoveryService
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryInfo, ZeroconfDiscoveryService
from majordom_integration_sdk.schemas import (
    CredentialsType,
    DeviceParameterChange,
    ProvidedCredentials,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterVisibility

from . import improv, mapper
from .bluetooth import ESPhomeBLESource, ble_source_of
from .connection import ESPhomeDeviceConnection, describe_error
from .models import (
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ESPhomeSettings,
    ParameterSpec,
)

logger = logging.getLogger(__name__)

NODE_APPEAR_TIMEOUT = 60.0  # seconds for a node to announce itself over mDNS once it has joined Wi-Fi


class WiFiCredentials(Protocol):
    ssid: str
    password: str


@dataclass
class _DiscoveryData:
    addresses: list[str]
    port: int
    mac: str | None
    node_name: str
    mdns_name: str
    keyless: bool  # encryption is on but no key is set: the integration gives the node one at pairing
    zero_psk: bool  # the keyless node takes its key over the zero PSK (older firmware: only in plaintext)


@dataclass
class _ImprovNode:
    """An unprovisioned node, waiting for Wi-Fi over BLE."""

    name: str  # its node name, as far as BLE carries it
    device: BLEDevice
    advertisement: AdvertisementData


class ESPhomeController(AbstractController[ESPhomeDevice, ESPhomeParameter]):
    name = "esphome"
    # Devices on Thread reach the hub through a border router; Wi-Fi devices need nothing.
    needs = [Need("thread", ["otbr-rest"], required=False)]
    settings = ESPhomeSettings

    def __init__(self, dependencies: AbstractController.Dependencies) -> None:
        super().__init__(dependencies)
        self._connections: dict[UUID, ESPhomeDeviceConnection] = {}
        self._ble_sources: dict[UUID, ESPhomeBLESource] = {}  # the nodes lending their Bluetooth radio to the hub
        self._discoveries: dict[UUID, Discovery] = {}
        self._discovery_data: dict[UUID, _DiscoveryData] = {}
        self._discovery_names: dict[str, UUID] = {}  # mDNS record name -> discovery id, for goodbyes
        self._zeroconf_cancel: Any | None = None
        self._ble_cancel: Any | None = None
        self._improv_nodes: dict[UUID, _ImprovNode] = {}
        self._ble_names: dict[str, str] = {}  # BLE address -> advertised name: ESPHome sends it in few adverts
        self._joining: dict[str, asyncio.Future[Discovery]] = {}  # BLE name -> the node's mDNS discovery, once seen
        self._paired: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._states: dict[UUID, dict[int, EntityState]] = {}

    @property
    def _settings(self) -> ESPhomeSettings:
        settings = self.dependencies.settings
        return settings if isinstance(settings, ESPhomeSettings) else ESPhomeSettings()

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
        self._ble_cancel = self.dependencies.ble_discovery_service.register(self, {UUID(improv.SERVICE_UUID)})
        await self._load_paired_devices()

    async def stop(self) -> None:
        logger.info("Stopping ESPHome integration")
        if self._zeroconf_cancel:
            self._zeroconf_cancel()
            self._zeroconf_cancel = None
        if self._ble_cancel:
            self._ble_cancel()
            self._ble_cancel = None

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
            # a key is configured: the user's, unless it is one this integration gave the node in an earlier attempt
            needs_key = "api_encryption" in properties and await self._kept_key(device_id) is None

            discovery = Discovery(
                id=device_id,
                integration=self.name,
                transport="tcp",
                device_manufacturer="esphome",
                device_name=properties.get("friendly_name") or node_name,
                device_category=None,  # a node may host anything
                device_icon=None,
                expected_credentials_options=[CredentialsType.secret if needs_key else CredentialsType.none],
            )
            keyless = "api_encryption_supported" in properties
            zero_psk = properties.get("api_provisioning") == "zero-psk"
            self._discovery_data[device_id] = _DiscoveryData(
                addresses, port, mac, node_name, service_info.name, keyless, zero_psk
            )
            self._discovery_names[service_info.name] = device_id
            for ble_name, joined in self._joining.items():
                if improv.is_node(ble_name, node_name) and not joined.done():
                    joined.set_result(discovery)
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

    # BLEDiscoveryListener: unprovisioned nodes advertising Improv

    async def ble_did_discover_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo) -> None:
        await self._on_improv_advert(info)

    async def ble_did_update_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo) -> None:
        await self._on_improv_advert(info)

    async def ble_did_remove_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo) -> None:
        name = self._ble_names.pop(info.device.address, None)
        device_id = self.device_uuid(f"improv:{name}") if name else None
        if device_id is not None and self._discoveries.pop(device_id, None) is not None:
            self._improv_nodes.pop(device_id, None)
            await self.dependencies.output.controller_did_lose_discovery(self, device_id)

    async def _on_improv_advert(self, info: BLEDiscoveryInfo) -> None:
        """Offer a node waiting for Wi-Fi. Improv carries no identity but the node name, so the discovery is keyed by
        it (the BLE address may be random, and differs from the MAC the node is known by once on the network)."""
        try:
            name = info.advertisement.local_name or self._ble_names.get(info.device.address)
            if name is None or not improv.waits_for_wifi(info.advertisement):
                return
            self._ble_names[info.device.address] = name
            device_id = self.device_uuid(f"improv:{name}")
            self._improv_nodes[device_id] = _ImprovNode(name, info.device, info.advertisement)
            if device_id in self._discoveries:  # adverts repeat all the time: keep the discovery (and its last error)
                return
            discovery = Discovery(
                id=device_id,
                integration=self.name,
                transport="ble",
                device_manufacturer="esphome",
                device_name=name,
                device_category=None,
                device_icon=None,
                expected_credentials_options=[CredentialsType.none],  # the Hub gives it its own Wi-Fi
            )
            self._discoveries[device_id] = discovery
            await self.dependencies.output.controller_did_receive_discovery(self, discovery)
            logger.debug("Discovered unprovisioned ESPHome node %s over BLE", name)
        except Exception:
            logger.exception("Error handling BLE discovery")

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
        await self._save_integration_data(device_id, addresses=refreshed)
        if connection := self._connections.get(device_id):
            connection.addresses = refreshed

    async def _save_integration_data(self, device_id: UUID, **changes: Any) -> None:
        async with self.dependencies.make_device_repository() as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is not None:
                data = device.integration_data.model_copy(update=changes)
                await repo.save(device.model_copy(update={"integration_data": data}))

    async def _load_paired_devices(self) -> None:
        async with self.dependencies.make_device_repository() as repo:
            devices = await repo.get_all(ESPhomeDevice)
        for device in devices:
            if device.integration_data.addresses:  # else the Hub created it, but its pairing never completed
                self._paired.add(device.id)
                self._connect(device)

    def _connect(self, device: ESPhomeDevice) -> None:
        data = device.integration_data
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
            on_client=self._on_client if self._settings.bluetooth_proxies else None,
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

    async def _on_client(self, device_id: UUID, client: APIClient | None) -> None:
        """A node connected: lend its Bluetooth radio, if it has one to lend. A node gone: take the radio back."""
        ble = self.dependencies.ble_discovery_service
        if source := self._ble_sources.pop(device_id, None):
            await ble.remove_source(source.name)
        if client is not None and (source := await ble_source_of(client)):
            self._ble_sources[device_id] = source
            await ble.add_source(source)

    async def _on_addresses(self, device_id: UUID, addresses: list[str]) -> None:
        if device_id in self._connections:  # not while pairing: the device is saved with the final order
            await self._save_integration_data(device_id, addresses=addresses)

    async def _update_device(self, device_id: UUID, **changes: Any) -> ESPhomeDevice | None:
        async with self.dependencies.make_device_repository() as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is None:
                return None
            if any(getattr(device, field) != value for field, value in changes.items()):
                device = device.model_copy(update=changes)
                await repo.save(device)
            return device

    async def _kept_key(self, device_id: UUID) -> str | None:
        device = await self._update_device(device_id)
        return device.integration_data.encryption_key if device is not None else None

    async def _provision_key(self, conn: ESPhomeDeviceConnection, data: _DiscoveryData, key: str | None) -> str:
        """Give a keyless node a key of our own (or the one kept from an earlier attempt). It is kept before it is
        set: from then on the node accepts nothing else, so losing it would lock the Hub out until a factory reset."""
        key = key or base64.b64encode(secrets.token_bytes(32)).decode()
        await self._save_integration_data(conn.device_id, encryption_key=key)
        await conn.provision_key(key, ZERO_NOISE_PSK if data.zero_psk else None)
        data.keyless = False  # whatever mDNS still announces, the node requires the key now
        return key

    async def _on_state(self, device_id: UUID, entity: EntityInfo, state: EntityState) -> None:
        self._states.setdefault(device_id, {})[state.key] = state  # kept while pairing: the snapshot then reports them
        if device_id not in self._connections:  # still pairing, or already unpaired: the Hub does not know the device
            return
        try:
            events = self._events(device_id, entity, state)
            if events:
                await self.dependencies.output.controller_did_receive_events(self, events)
        except Exception:
            logger.exception("Error handling state for %s", entity.name)

    def _events(self, device_id: UUID, entity: EntityInfo, state: EntityState) -> list[DeviceParameterChange]:
        component = mapper.component_type_of(entity)
        if component is None:
            return []
        exposed = {spec.sub_field: spec for spec in mapper.parameter_specs(entity, component)}
        events = []
        for sub_field, value in mapper.state_values(entity, state).items():
            spec = exposed.get(sub_field)
            if spec is None:
                continue
            if spec.fields:  # a struct value: keyed by the sub-parameter ids, as the SDK specifies
                value = {
                    str(self._parameter_id(device_id, entity, f"{sub_field}.{name}")): v for name, v in value.items()
                }
            parameter_id = self._parameter_id(device_id, entity, sub_field)
            events.append(DeviceParameterChange(device_id=device_id, parameter_id=parameter_id, value=value))
        return events

    @staticmethod
    def _visibility(entity: EntityInfo, spec: ParameterSpec) -> ParameterVisibility:
        """The entity's own visibility, unless the spec asks for a less prominent one (and the entity allows it)."""
        visibility = mapper.visibility_of(entity)
        if spec.visibility == ParameterVisibility.system:  # infrastructure is hidden whatever the entity says
            return spec.visibility
        return spec.visibility if visibility == ParameterVisibility.user and spec.visibility else visibility

    def _parameter_id(self, device_id: UUID, entity: EntityInfo, sub_field: str) -> UUID:
        return self.parameter_uuid(device_id, f"{entity.object_id}_{sub_field}")

    async def _report_snapshot(self, device_id: UUID) -> None:
        """Report the last known value of every parameter in a single batch, the kept settings included."""
        connection = self._connections[device_id]
        entities = connection.get_entities()
        events = [
            event
            for key, state in self._states.get(device_id, {}).items()
            if key in entities
            for event in self._events(device_id, entities[key], state)
        ]
        device = await self._update_device(device_id)
        settings = device.integration_data.settings if device is not None else {}
        for entity in entities.values():
            for field, value in settings.get(entity.object_id, {}).items():
                if value is None:  # a cleared setting
                    continue
                parameter_id = self._parameter_id(device_id, entity, field)
                events.append(DeviceParameterChange(device_id=device_id, parameter_id=parameter_id, value=value))
        if events:
            await self.dependencies.output.controller_did_receive_events(self, events)

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None) -> UUID | None:
        if discovery.id in self._improv_nodes:
            return await self._pair_over_ble(discovery)
        await self._pair_over_api(discovery, credentials)
        return None

    async def _pair_over_ble(self, discovery: Discovery) -> UUID:
        """Give the node the Hub's Wi-Fi, wait for it on the network, then pair it like any other node. The Hub's device
        takes the node's own identity (from its MAC), which is what it is found by from then on; it is returned."""
        logger.info("Setting up ESPHome device %s over BLE", discovery.device_name)
        node = self._improv_nodes[discovery.id]
        try:
            wifi = await self._wifi_credentials()
            async with self.dependencies.make_device_repository() as repo:
                hub_device = await repo.get(discovery.id, as_=ESPhomeDevice)
            if hub_device is None:
                raise LookupError("The Hub has not created a device for this discovery")
            joined = self._joining[node.name] = asyncio.get_running_loop().create_future()
            try:
                await improv.provision(
                    node.device,
                    node.advertisement,
                    wifi.ssid,
                    wifi.password,
                    on_authorization_required=lambda: self._report_error(discovery.id, improv.PRESS_BUTTON),
                )
                async with asyncio.timeout(NODE_APPEAR_TIMEOUT):
                    on_network = await joined
            except TimeoutError:
                raise TimeoutError(f"The device joined {wifi.ssid} but did not show up on the network") from None
            finally:
                self._joining.pop(node.name, None)
            async with self.dependencies.make_device_repository() as repo:
                await repo.save(hub_device.model_copy(update={"id": on_network.id}), previous_id=discovery.id)
            await self._pair_over_api(on_network, credentials=None)
        except Exception as exc:
            logger.exception("Setting up %s over BLE failed", discovery.device_name)
            reason = str(exc) if isinstance(exc, ValueError | LookupError | TimeoutError | improv.SetupError) else None
            await self._report_error(discovery.id, reason or describe_error(exc.__cause__ or exc))
            raise
        self._discoveries.pop(discovery.id, None)
        self._improv_nodes.pop(discovery.id, None)
        # the Hub paired the node through its BLE discovery: the one it announced over mDNS is gone too
        await self.dependencies.output.controller_did_lose_discovery(self, on_network.id)
        return on_network.id

    async def _wifi_credentials(self) -> WiFiCredentials:
        # injected by the Hub from SDK feature/wifi-credentials on; older Hubs have none
        provider: Callable[[], Awaitable[WiFiCredentials | None]] | None = getattr(
            self.dependencies, "wifi_credentials", None
        )
        credentials = await provider() if provider is not None else None
        if credentials is None:
            raise ValueError("The Hub is not connected to Wi-Fi, so it has no network to give the device")
        return credentials

    async def _report_error(self, discovery_id: UUID, reason: str) -> None:
        """Show the user why pairing a discovery failed (or what it waits for), while it is still offered."""
        if (known := self._discoveries.get(discovery_id)) is not None:
            failed = known.model_copy(update={"last_error": reason})
            self._discoveries[discovery_id] = failed
            await self.dependencies.output.controller_did_update_discovery(self, failed)

    async def _pair_over_api(self, discovery: Discovery, credentials: ProvidedCredentials | None) -> None:
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
            async with self.dependencies.make_device_repository() as repo:
                hub_device = await repo.get(
                    discovery.id, as_=ESPhomeDevice
                )  # the Hub creates the device before pairing
            if hub_device is None:
                raise LookupError("The Hub has not created a device for this discovery")

            encryption_key = encryption_key or hub_device.integration_data.encryption_key

            conn = self._make_connection(
                discovery.id, integration_data.addresses, integration_data.port, encryption_key
            )
            if integration_data.keyless:
                encryption_key = await self._provision_key(conn, integration_data, encryption_key)
            elif encryption_key is None:
                logger.warning(
                    "%s accepts unencrypted API connections only: add `api: encryption:` to its YAML",
                    discovery.device_name,
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
            self._states.pop(discovery.id, None)
            logger.exception("Pairing %s failed", discovery.id)
            reason = str(exc) if isinstance(exc, ValueError | LookupError) else describe_error(exc.__cause__ or exc)
            failed = known.model_copy(update={"last_error": reason})
            self._discoveries[discovery.id] = failed
            await self.dependencies.output.controller_did_update_discovery(self, failed)
            raise

    def _build_parameter(
        self, device_id: UUID, entity: EntityInfo, component: str, spec: ParameterSpec, parent: str | None = None
    ) -> ESPhomeParameter:
        path = (
            f"{parent}.{spec.sub_field}" if parent else spec.sub_field
        )  # a sub-parameter's id is scoped to its parent
        label = spec.sub_field.replace("_", " ")
        name = label.capitalize() if parent else entity.name if spec.sub_field == "state" else f"{entity.name} {label}"
        return ESPhomeParameter(
            id=self._parameter_id(device_id, entity, path),
            name=name,
            data_type=spec.data_type,
            role=spec.role,
            unit=spec.unit,
            min_value=spec.min_value,
            max_value=spec.max_value,
            min_step=spec.min_step,
            valid_values=spec.valid_values,
            visibility=self._visibility(entity, spec),
            fields=[self._build_parameter(device_id, entity, component, child, path) for child in spec.fields] or None,
            integration_data=ESPhomeParameterIntegrationData(
                entity_name=entity.name,
                object_id=entity.object_id,
                component_type=component,
                parameter_type=mapper.PARAMETER_TYPE.get(component, mapper.ESPhomeParameterType.STATE),
                service_key=entity.key,
                sub_field=spec.sub_field,
            ),
        )

    async def _keep_setting(
        self, device_id: UUID, object_id: str, field: str, parameter: ESPhomeParameter, value: Any
    ) -> None:
        kept = mapper.local_value(parameter, value)
        async with self.dependencies.make_device_repository() as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is None:
                raise ValueError(f"Unknown device {device_id}")
            settings = {**device.integration_data.settings}
            settings[object_id] = {**settings.get(object_id, {}), field: kept}
            data = device.integration_data.model_copy(update={"settings": settings})
            await repo.save(device.model_copy(update={"integration_data": data}))
        event = DeviceParameterChange(device_id=device_id, parameter_id=parameter.id, value=kept)
        await self.dependencies.output.controller_did_receive_events(self, [event])

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
        data = parameter.integration_data
        if mapper.is_local(parameter):  # a setting kept here: stored, and echoed so the Hub stores it too
            await self._keep_setting(device.id, data.object_id or "", data.sub_field or "", parameter, command.value)
            return

        connection = self._connections.get(device.id)
        if connection is None or not connection.ready:
            await self._update_device(device.id, last_error=f"{parameter.name} could not be set: device not connected")
            raise ConnectionError("Device not connected")

        stored = await self._update_device(device.id)
        settings = stored.integration_data.settings.get(data.object_id or "", {}) if stored is not None else {}
        args = mapper.build_command_args(parameter, command.value, self._states.get(device.id, {}), settings)
        try:
            await connection.send_command(args["key"], data.component_type, args)
        except ConnectionError:
            await self._update_device(device.id, last_error=f"{parameter.name} could not be set: device not connected")
            raise
        await self._update_device(device.id, last_error=None)
