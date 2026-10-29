"""ESPHome integration controller for MajorDom."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from aioesphomeapi import ZERO_NOISE_PSK, APIClient, EntityInfo, EntityState
from majordom_integration_sdk import (
    AbstractController,
    MissingCredentialsError,
    Need,
    PeripheralChange,
    Platform,
    WifiCredentials,
)
from majordom_integration_sdk.capabilities import (
    BLE_ROUTER,
    ZEROCONF,
    BLEDiscoveryInfo,
    BLERouter,
    Zeroconf,
    ZeroconfDiscoveryInfo,
)
from majordom_integration_sdk.files import read_json, write_json
from majordom_integration_sdk.peripherals import BLEProxy
from majordom_integration_sdk.schemas import (
    CredentialsType,
    DeviceParameterChange,
    ProvidedCredentials,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterVisibility
from pydantic import BaseModel, Field

from . import improv, mapper
from .bluetooth import ble_source_of
from .connection import ESPhomeDeviceConnection, describe_error
from .models import (
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ParameterSpec,
)

logger = logging.getLogger(__name__)

NODE_APPEAR_TIMEOUT = 60.0  # seconds for a node to announce itself over mDNS once it has joined Wi-Fi


@dataclass
class _DiscoveryData:
    addresses: list[str]
    port: int
    mac: str | None
    keyless: bool  # encryption is on but no key is set: the integration gives the node one at pairing
    zero_psk: bool  # the keyless node takes its key over the zero PSK (older firmware: only in plaintext)


@dataclass
class _ImprovNode:
    """An unprovisioned node, waiting for Wi-Fi over BLE."""

    name: str  # its node name, as far as BLE carries it
    address: str


class ESPhomeController(AbstractController[ESPhomeDevice, ESPhomeParameter, "ESPhomeController.Settings"]):
    name = "ESPHome"  # its key, the slug "esphome", scopes its devices
    # Nodes are found over mDNS. Devices on Thread reach the hub through a border router; Wi-Fi devices need nothing.
    # The router sets unprovisioned nodes up over Improv BLE; the nodes that proxy Bluetooth are offered back to it.
    needs = [
        ZEROCONF,
        Need("thread", ["otbr-rest"], is_required=False),
        replace(BLE_ROUTER, is_required=False),
    ]

    class Settings(BaseModel):
        is_bluetooth_proxy_enabled: bool = Field(
            True,
            title="Bluetooth proxies",
            description="Reach Bluetooth devices through ESPHome devices with bluetooth_proxy. A device serves one hub "
            "at a time: turn this off only if another hub needs its Bluetooth proxy.",
        )

    def __init__(self, platform: Platform, *, settings: ESPhomeController.Settings | None = None):
        super().__init__(platform, settings=settings)
        self._connections: dict[UUID, ESPhomeDeviceConnection] = {}
        self._proxies: dict[UUID, BLEProxy] = {}  # the nodes lending their Bluetooth radio to the hub
        self._discoveries: dict[UUID, Discovery] = {}
        self._discovery_data: dict[UUID, _DiscoveryData] = {}
        self._discovery_names: dict[str, UUID] = {}  # mDNS record name -> discovery id, for goodbyes
        self._zeroconf_cancel: Callable[[], None] | None = None
        self._ble_cancel: Callable[[], None] | None = None
        self._improv_nodes: dict[UUID, _ImprovNode] = {}
        self._ble_names: dict[str, str] = {}  # BLE address -> advertised name: ESPHome sends it in few adverts
        self._joining: dict[str, asyncio.Future[Discovery]] = {}  # BLE name -> the node's mDNS discovery, once seen
        self._paired: set[UUID] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._states: dict[UUID, dict[int, EntityState]] = {}
        self._run: object | None = None  # from start() to stop(): a pairing finishing in another run adopts nothing

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self._discoveries.copy()

    @property
    def provides(self) -> list[BLEProxy]:
        return list(self._proxies.values())

    @property
    def device_type(self) -> type[ESPhomeDevice]:
        return ESPhomeDevice

    @property
    def parameter_type(self) -> type[ESPhomeParameter]:
        return ESPhomeParameter

    # Lifecycle

    async def start(self):
        logger.info("Starting ESPHome integration")
        self._run = object()
        await self._load_paired_devices()  # first: DNS-SD tells the nodes in its cache at once, the paired ones too

        zeroconf = self.peripherals.instance(ZEROCONF)
        self._zeroconf_cancel = zeroconf.register(self, services={"_esphomelib._tcp.local."})
        self._listen_for_improv()

    async def stop(self):
        logger.info("Stopping ESPHome integration")
        self._run = None
        if self._zeroconf_cancel:
            self._zeroconf_cancel()
            self._zeroconf_cancel = None
        if self._ble_cancel:
            self._ble_cancel()
            self._ble_cancel = None

        tasks = list(self._tasks)  # the setups over Bluetooth too
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for connection in list(self._connections.values()):
            await connection.stop()
        self._connections.clear()
        for device_id in list(self._proxies):
            await self._on_client(device_id, None)
        self._states.clear()
        self._paired.clear()
        # heard anew after a start, which reports them again; a node that left meanwhile says no goodbye
        self._discoveries.clear()
        self._discovery_data.clear()
        self._discovery_names.clear()
        self._improv_nodes.clear()
        self._ble_names.clear()

    async def needed_peripheral_did_change(self, change: PeripheralChange) -> bool:
        """The router is followed in place, and a border router only carries the Thread devices to the hub: neither
        restarts it. Another DNS-SD does."""
        if change.family == "ble":
            await self._let_go_of_router()
            self._listen_for_improv()
        return change.family in ("ble", "thread")

    # Hub -> device

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None) -> UUID | None:
        run = self._run
        if discovery.id in self._improv_nodes:
            return await self._pair_over_ble(discovery, run)
        await self._pair_over_api(discovery, credentials, run)
        return None

    async def unpair(self, device: ESPhomeDevice):
        connection = self._connections.pop(device.id, None)
        self._states.pop(device.id, None)
        self._paired.discard(device.id)
        if connection:
            await connection.stop()
        # The Hub removes the device record after unpair; the repository protocol
        # intentionally does not expose delete() to integrations.

    async def identify(self, device: ESPhomeDevice):
        # ESPHome has no identify action, so there is nothing to send to the device
        logger.info(f"Identify called for {device.id}: not supported by ESPHome")

    async def fetch(self, device: ESPhomeDevice):
        if device.id not in self._connections:
            raise ConnectionError("Device not connected")
        await self._report_snapshot(device.id)

    async def send_command(
        self,
        command: DeviceCommand,
        device: ESPhomeDevice,
        parameter: ESPhomeParameter,
    ):
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

    # ZeroconfDiscoveryListener: called by the SDK's discovery service

    async def zeroconf_did_discover_service(self, zeroconf: Zeroconf, info: ZeroconfDiscoveryInfo):
        await self._on_zeroconf_service(info)

    async def zeroconf_did_update_service(self, zeroconf: Zeroconf, info: ZeroconfDiscoveryInfo):
        await self._on_zeroconf_service(info)

    async def zeroconf_did_remove_service(self, zeroconf: Zeroconf, type_: str, name: str):
        device_id = self._discovery_names.pop(name, None)
        if device_id is not None and self._discoveries.pop(device_id, None) is not None:
            self._discovery_data.pop(device_id, None)
            await self.output.controller_did_lose_discovery(self, device_id)

    # BLEDiscoveryListener: unprovisioned nodes advertising Improv

    async def ble_did_discover_device(self, ble: BLERouter, info: BLEDiscoveryInfo):
        await self._on_improv_advert(info)

    async def ble_did_update_device(self, ble: BLERouter, info: BLEDiscoveryInfo):
        await self._on_improv_advert(info)

    async def ble_did_remove_device(self, ble: BLERouter, info: BLEDiscoveryInfo):
        if self._ble_names.get(info.device.address) in self._joining:  # set up: it stops advertising once on Wi-Fi
            return
        await self._lose_improv_node(info.device.address)

    # Private: discovery

    async def _on_zeroconf_service(self, service_info: ZeroconfDiscoveryInfo):
        try:
            node_name = service_info.name.removesuffix(f".{service_info.type_}")
            properties = service_info.decoded_properties
            mac = properties.get("mac")
            device_id = self.device_uuid(mac or node_name)
            addresses = self._announced_addresses(service_info, node_name)
            if device_id in self._paired:
                await self._refresh_addresses(device_id, addresses)
                await self._refuse_setup(node_name, device_id)
                return
            port = service_info.port or 6053
            # a key is configured: the user's, unless it is one this integration gave the node in an earlier attempt.
            # Even then the user may enter one: the node may have been reflashed with a key of its own since.
            if "api_encryption" not in properties:
                credentials_options = [CredentialsType.none]
            elif await self._kept_key(device_id, mac) is None:
                credentials_options = [CredentialsType.secret]
            else:
                credentials_options = [CredentialsType.none, CredentialsType.secret]

            discovery = Discovery(
                id=device_id,
                integration=self.key,
                transport="tcp",
                device_manufacturer="esphome",
                device_name=properties.get("friendly_name") or node_name,
                device_category=None,  # a node may host anything
                device_icon=None,
                expected_credentials_options=credentials_options,
            )
            keyless = "api_encryption_supported" in properties
            zero_psk = properties.get("api_provisioning") == "zero-psk"
            self._discovery_data[device_id] = _DiscoveryData(addresses, port, mac, keyless, zero_psk)
            self._discovery_names[service_info.name] = device_id
            for ble_name, joined in self._joining.items():
                if improv.is_node(ble_name, node_name) and not joined.done():
                    joined.set_result(discovery)
            known = self._discoveries.get(device_id)
            if known == discovery:
                return
            self._discoveries[device_id] = discovery
            if known is None:
                await self.output.controller_did_receive_discovery(self, discovery)
            else:
                await self.output.controller_did_update_discovery(self, discovery)
            logger.debug(f"Discovered ESPHome node {node_name} at {addresses}:{port}")
        except Exception:
            logger.exception("Error handling zeroconf discovery")

    @property
    def _router(self) -> BLERouter | None:
        return self.peripherals.instance(BLE_ROUTER)

    async def _let_go_of_router(self):
        """Stop listening to the router, and lose the nodes it heard: the next one tells what it hears."""
        if self._ble_cancel:
            self._ble_cancel()
            self._ble_cancel = None
        lost = [device_id for device_id in self._improv_nodes if self._discoveries.pop(device_id, None) is not None]
        self._improv_nodes.clear()
        self._ble_names.clear()
        for device_id in lost:
            await self.output.controller_did_lose_discovery(self, device_id)

    def _listen_for_improv(self):
        """Hear unprovisioned nodes through the router bound now, if there is one."""
        router = self._router
        self._ble_cancel = router.register(self, {UUID(improv.SERVICE_UUID)}) if router else None

    async def _lose_improv_node(self, address: str):
        name = self._ble_names.pop(address, None)
        device_id = self.device_uuid(f"improv:{name}") if name else None
        if device_id is not None and self._discoveries.pop(device_id, None) is not None:
            self._improv_nodes.pop(device_id, None)
            await self.output.controller_did_lose_discovery(self, device_id)

    async def _on_improv_advert(self, info: BLEDiscoveryInfo):
        """Offer a node waiting for Wi-Fi. Improv carries no identity but the node name, so the discovery is keyed by
        it (the BLE address may be random, and differs from the MAC the node is known by once on the network)."""
        try:
            name = info.advertisement.local_name or self._ble_names.get(info.device.address)
            if name is None or not improv.waits_for_wifi(info.advertisement):
                return
            self._ble_names[info.device.address] = name
            device_id = self.device_uuid(f"improv:{name}")
            self._improv_nodes[device_id] = _ImprovNode(name, info.device.address)
            if device_id in self._discoveries:  # adverts repeat all the time: keep the discovery (and its last error)
                return
            discovery = Discovery(
                id=device_id,
                integration=self.key,
                transport="ble",
                device_manufacturer="esphome",
                device_name=name,
                device_category=None,
                device_icon=None,
                expected_credentials_options=[CredentialsType.none],  # the Hub gives it its own Wi-Fi
            )
            self._discoveries[device_id] = discovery
            await self.output.controller_did_receive_discovery(self, discovery)
            logger.debug(f"Discovered unprovisioned ESPHome node {name} over BLE")
        except Exception:
            logger.exception("Error handling BLE discovery")

    @staticmethod
    def _announced_addresses(info: ZeroconfDiscoveryInfo, node_name: str) -> list[str]:
        """Hostname first (it follows the device through DHCP changes), then the announced IPs."""
        hostname = info.server.rstrip(".") if info.server else None
        addresses = [hostname, *(info.parsed_addresses or [])] if hostname else list(info.parsed_addresses or [])
        return list(dict.fromkeys(addresses)) or [f"{node_name}.local"]

    async def _refresh_addresses(self, device_id: UUID, announced: list[str]):
        """A paired device announced itself again: learn its new addresses, forget the ones it left."""
        device = await self._update_device(device_id)
        if device is None:
            return
        known = device.integration_data.addresses
        refreshed = [a for a in announced if a not in known] + [a for a in known if a in announced]
        if refreshed == known:
            return
        logger.info(f"Addresses of {device_id} changed: {known} -> {refreshed}")
        await self._save_integration_data(device_id, addresses=refreshed)
        if connection := self._connections.get(device_id):
            connection.addresses = refreshed

    async def _refuse_setup(self, node_name: str, device_id: UUID):
        """A node set up over BLE announced itself as a paired one (its Wi-Fi was reset): it is back, and its device
        reconnects. Its setup fails rather than replace that device."""
        joining = [joined for ble_name, joined in self._joining.items() if improv.is_node(ble_name, node_name)]
        if not joining:
            return
        device = await self._update_device(device_id)
        name = device.name if device is not None else node_name
        for joined in joining:
            if not joined.done():
                joined.set_exception(ValueError(f"The device is already paired as {name}: it is back on the network"))

    # Private: connections and state

    async def _load_paired_devices(self):
        async with self.platform.device_repository(self) as repo:
            devices = await repo.get_all(ESPhomeDevice)
        for device in devices:
            if device.integration_data.addresses:  # else the Hub created it, but its pairing never completed
                self._paired.add(device.id)
                self._connect(device)

    def _connect(self, device: ESPhomeDevice):
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
            on_client=self._on_client if self.settings.is_bluetooth_proxy_enabled else None,
        )

    def _spawn[T](self, coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Run in the background, keeping a reference so the task is neither collected nor left behind on stop()."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _on_availability(self, device_id: UUID, available: bool, reason: str | None):
        if device_id not in self._connections:  # still pairing, or already unpaired
            return
        await self._update_device(device_id, available=available, last_error=None if available else reason)
        if available:
            await self.output.controller_did_connect_device(self, device_id)
        else:
            await self.output.controller_did_lose_device(self, device_id)

    async def _on_client(self, device_id: UUID, client: APIClient | None):
        """A paired node connected: lend its Bluetooth radio, if it has one to lend, as a ``ble/esphome-proxy`` the
        router takes; a node being paired serves no one until it is. A node gone: take the radio back."""
        if client is None:
            if self._proxies.pop(device_id, None):
                await self.output.controller_did_change_provided(self)
        elif device_id in self._connections and (source := await ble_source_of(client)):
            self._proxies[device_id] = BLEProxy(source, name=source.mac)
            await self.output.controller_did_change_provided(self)

    async def _on_addresses(self, device_id: UUID, addresses: list[str]):
        if device_id in self._connections:  # not while pairing: the device is saved with the final order
            await self._save_integration_data(device_id, addresses=addresses)

    async def _update_device(self, device_id: UUID, **changes: Any) -> ESPhomeDevice | None:
        async with self.platform.device_repository(self) as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is None:
                return None
            if any(getattr(device, field) != value for field, value in changes.items()):
                device = device.model_copy(update=changes)
                await repo.save(device)
            return device

    async def _save_integration_data(self, device_id: UUID, **changes: Any):
        async with self.platform.device_repository(self) as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is not None:
                data = device.integration_data.model_copy(update=changes)
                await repo.save(device.model_copy(update={"integration_data": data}))

    async def _on_state(self, device_id: UUID, entity: EntityInfo, state: EntityState):
        self._states.setdefault(device_id, {})[state.key] = state  # kept while pairing: the snapshot then reports them
        if device_id not in self._connections:  # still pairing, or already unpaired: the Hub does not know the device
            return
        try:
            events = self._events(device_id, entity, state)
            if events:
                await self.output.controller_did_receive_events(self, events)
        except Exception:
            logger.exception(f"Error handling state for {entity.name}")

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

    async def _report_snapshot(self, device_id: UUID):
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
            await self.output.controller_did_receive_events(self, events)

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

    async def _keep_setting(self, device_id: UUID, object_id: str, field: str, parameter: ESPhomeParameter, value: Any):
        kept = mapper.local_value(parameter, value)
        async with self.platform.device_repository(self) as repo:
            device = await repo.get(device_id, as_=ESPhomeDevice)
            if device is None:
                raise ValueError(f"Unknown device {device_id}")
            settings = {**device.integration_data.settings}
            settings[object_id] = {**settings.get(object_id, {}), field: kept}
            data = device.integration_data.model_copy(update={"settings": settings})
            await repo.save(device.model_copy(update={"integration_data": data}))
        event = DeviceParameterChange(device_id=device_id, parameter_id=parameter.id, value=kept)
        await self.output.controller_did_receive_events(self, [event])

    # Private: pairing

    async def _pair_over_ble(self, discovery: Discovery, run: object | None) -> UUID:
        """Give the node the Hub's Wi-Fi, wait for it on the network, then pair it like any other node. The Hub's device
        takes the node's own identity (from its MAC), which is what it is found by from then on; it is returned."""
        logger.info(f"Setting up ESPHome device {discovery.device_name} over BLE")
        node = self._improv_nodes[discovery.id]
        try:
            wifi = self._wifi_credentials()
            async with self.platform.device_repository(self) as repo:
                hub_device = await repo.get(discovery.id, as_=ESPhomeDevice)
            if hub_device is None:
                raise LookupError("The Hub has not created a device for this discovery")
            if (router := self._router) is None:
                raise LookupError("The Hub has no Bluetooth to set the device up with")
            setup = self._spawn(self._join_wifi(router, node, wifi, discovery.id))  # stop() ends it at once
            try:
                on_network = await setup
            except asyncio.CancelledError:
                self._refuse_if_stopped(run)  # stop() ended it
                raise  # the Hub's request went away
            self._refuse_if_stopped(run)
            async with self.platform.device_repository(self) as repo:
                await repo.save(hub_device.model_copy(update={"id": on_network.id}), previous_id=discovery.id)
            await self._pair_over_api(on_network, None, run)
        except MissingCredentialsError:
            raise  # the Hub returns it to the app's pairing request
        except Exception as exc:
            logger.exception(f"Setting up {discovery.device_name} over BLE failed")
            reason = str(exc) if isinstance(exc, ValueError | LookupError | TimeoutError | improv.SetupError) else None
            await self._report_error(discovery.id, reason or describe_error(exc.__cause__ or exc))
            router = self._router
            if router is None or node.address not in {info.device.address for info in router.devices()}:
                await self._lose_improv_node(node.address)  # its removal came while it was set up: none comes again
            raise
        self._discoveries.pop(discovery.id, None)
        self._improv_nodes.pop(discovery.id, None)
        # the Hub paired the node through its BLE discovery: the one it announced over mDNS is gone too
        await self.output.controller_did_lose_discovery(self, on_network.id)
        return on_network.id

    async def _join_wifi(
        self, router: BLERouter, node: _ImprovNode, wifi: WifiCredentials, discovery_id: UUID
    ) -> Discovery:
        """Give the node the Hub's Wi-Fi over Improv, then wait for it on the network: its mDNS discovery."""
        joined = self._joining[node.name] = asyncio.get_running_loop().create_future()
        try:
            await improv.provision(
                router,
                node.address,
                wifi.ssid,
                wifi.password,
                on_authorization_required=lambda: self._report_error(discovery_id, improv.PRESS_BUTTON),
            )
            async with asyncio.timeout(NODE_APPEAR_TIMEOUT):
                return await joined
        except TimeoutError:
            raise TimeoutError(f"The device joined {wifi.ssid} but did not show up on the network") from None
        finally:
            self._joining.pop(node.name, None)

    def _wifi_credentials(self) -> WifiCredentials:
        if (wifi := self.platform.wifi) is None:
            raise MissingCredentialsError(MissingCredentialsError.WIFI)
        return wifi

    async def _report_error(self, discovery_id: UUID, reason: str):
        """Show the user why pairing a discovery failed (or what it waits for), while it is still offered."""
        if (known := self._discoveries.get(discovery_id)) is not None:
            failed = known.model_copy(update={"last_error": reason})
            self._discoveries[discovery_id] = failed
            await self.output.controller_did_update_discovery(self, failed)

    async def _pair_over_api(self, discovery: Discovery, credentials: ProvidedCredentials | None, run: object | None):
        logger.info(f"Pairing ESPHome device: {discovery.id}")
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
            async with self.platform.device_repository(self) as repo:
                hub_device = await repo.get(
                    discovery.id, as_=ESPhomeDevice
                )  # the Hub creates the device before pairing
            if hub_device is None:
                raise LookupError("The Hub has not created a device for this discovery")

            encryption_key = (
                encryption_key or hub_device.integration_data.encryption_key or self._given_key(integration_data.mac)
            )

            conn = self._make_connection(
                discovery.id, integration_data.addresses, integration_data.port, encryption_key
            )
            if integration_data.keyless:
                encryption_key = await self._provision_key(conn, integration_data, encryption_key)
            elif encryption_key is None:
                logger.warning(
                    f"{discovery.device_name} accepts unencrypted API connections only: "
                    "add `api: encryption:` to its YAML"
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

            self._refuse_if_stopped(run)
            async with self.platform.device_repository(self) as repo:
                await repo.save(device)

            self._refuse_if_stopped(run)  # it stopped while the device was saved
            self._connections[discovery.id] = conn
            self._paired.add(discovery.id)
            if credentials_type == CredentialsType.secret:
                self._forget_given_key(integration_data.mac)

            await self.output.controller_did_connect_device(self, discovery.id)
            await self._report_snapshot(discovery.id)
            await conn.report_client()  # paired: its radio may serve the hub now
            self._discoveries.pop(discovery.id, None)
            self._discovery_data.pop(discovery.id, None)

        except asyncio.CancelledError:  # no one waits for the outcome: let go of the node
            await self._let_go_of_pairing(discovery.id, conn)
            raise
        except Exception as exc:
            await self._let_go_of_pairing(discovery.id, conn)
            logger.exception(f"Pairing {discovery.id} failed")
            if self._is_current(run):  # a stopped controller offers nothing
                reason = str(exc) if isinstance(exc, ValueError | LookupError) else describe_error(exc.__cause__ or exc)
                failed = known.model_copy(update={"last_error": reason})
                self._discoveries[discovery.id] = failed
                await self.output.controller_did_update_discovery(self, failed)
            raise

    async def _let_go_of_pairing(self, device_id: UUID, conn: ESPhomeDeviceConnection | None):
        """Undo a pairing that didn't finish: its node connection goes, also once taken in, so it can be tried again."""
        if conn is not None:
            await conn.stop()
            if self._connections.get(device_id) is conn:
                del self._connections[device_id]
                self._paired.discard(device_id)
        self._states.pop(device_id, None)

    def _refuse_if_stopped(self, run: object | None):
        """A pairing that finishes after stop() takes nothing, also once ESPHome runs again (that run may have other
        settings): the next start() connects what is paired anew. Adopting it into the new run may come later."""
        if not self._is_current(run):
            raise ConnectionError("ESPHome stopped while pairing")

    def _is_current(self, run: object | None) -> bool:
        return run is not None and run is self._run

    # Private: encryption keys

    async def _kept_key(self, device_id: UUID, mac: str | None) -> str | None:
        device = await self._update_device(device_id)
        return (device.integration_data.encryption_key if device is not None else None) or self._given_key(mac)

    @property
    def _keys_file(self) -> Path:
        return self.platform.documents_folder(self) / "keys.json"

    def _given_key(self, mac: str | None) -> str | None:
        """The key given to the node with ``mac``, kept apart from the Hub's device record too: the Hub deletes the
        record when a pairing fails, and after unpairing it, though the node requires the key from then on. Forgotten
        only once the node is paired with a key the user typed (it was reflashed with one of its own)."""
        return read_json(self._keys_file).get(mac) if mac else None

    def _forget_given_key(self, mac: str | None):
        keys = read_json(self._keys_file)
        if keys.pop(mac or "", None):
            write_json(self._keys_file, keys)

    async def _provision_key(self, conn: ESPhomeDeviceConnection, data: _DiscoveryData, key: str | None) -> str:
        """Give a keyless node a key of our own (or the one kept from an earlier attempt). It is kept before it is
        set: from then on the node accepts nothing else, so losing it would lock the Hub out until a factory reset."""
        key = key or base64.b64encode(secrets.token_bytes(32)).decode()
        await self._save_integration_data(conn.device_id, encryption_key=key)
        if data.mac:
            write_json(self._keys_file, {**read_json(self._keys_file), data.mac: key})
        await conn.provision_key(key, ZERO_NOISE_PSK if data.zero_psk else None)
        data.keyless = False  # whatever mDNS still announces, the node requires the key now
        return key
