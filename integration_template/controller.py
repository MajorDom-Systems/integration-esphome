"""ESPHome integration controller for MajorDom Hub."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID, NAMESPACE_DNS, uuid5

from majordom_integration_sdk.controller import AbstractController
from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import CredentialsValue, Discovery
from majordom_hub.schemas.parameter import ParameterDataType, ParameterRole, ParameterVisibility

from .connection import ESPhomeDeviceConnection
from .esphome_spec import get_max_value, get_min_step, get_min_value, get_unit
from . import mapper
from .models import (
    ESPhomeComponentType,
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ESPhomeParameterType,
)

logger = logging.getLogger(__name__)


class ESPhomeController(AbstractController[ESPhomeDevice, ESPhomeParameter]):
    """ESPHome integration controller."""

    def __init__(self, dependencies: AbstractController.Dependencies) -> None:
        super().__init__(dependencies)
        self._connections: dict[UUID, ESPhomeDeviceConnection] = {}
        self._discoveries: dict[UUID, Discovery] = {}
        self._zeroconf_cancel: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._state_cache: dict[UUID, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "esphome"

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
            listener=self._on_zeroconf_service,
            services={"_esphomelib._tcp.local."},
        )
        await self._load_paired_devices()

    async def stop(self) -> None:
        logger.info("Stopping ESPHome integration")
        if self._zeroconf_cancel:
            self._zeroconf_cancel()
            self._zeroconf_cancel = None

        async with self._lock:
            for conn in list(self._connections.values()):
                await conn.stop()
            self._connections.clear()



    async def _on_zeroconf_service(self, service_info: Any) -> None:
        try:
            name = getattr(service_info, "name", "Unknown")
            server = getattr(service_info, "server", None)
            addresses = getattr(service_info, "parsed_addresses", lambda *a, **k: [])()
            address = server or (addresses[0] if addresses else name)
            port = getattr(service_info, "port", 6053)
            properties = getattr(service_info, "properties", {}) or {}

            mac = properties.get(b"mac", b"").decode("utf-8", errors="ignore") or name
            unique_id = mac.replace(":", "").lower()
            device_id = uuid5(NAMESPACE_DNS, f"esphome_device_{unique_id}")

            if device_id in self._discoveries:
                return

            has_encryption = bool(properties.get(b"encryption"))
            discovery = Discovery(
                id=device_id,
                name=name,
                integration=self.name,
                integration_data={"address": address, "port": port, "requires_encryption": has_encryption},
                credentials="none",
                transport="tcp",
                device_manufacturer="esphome",
                device_name=name,
                device_category="light",
                device_icon="",
            )
            self._discoveries[device_id] = discovery
            await self.dependencies.output.controller_did_receive_discovery(self, discovery)
            logger.debug("Discovered ESPHome device: %s at %s:%s", name, address, port)
        except Exception:
            logger.exception("Error handling zeroconf discovery")



    async def _load_paired_devices(self) -> None:
        async with self.dependencies.make_device_repository() as repo:
            devices = [d for d in await repo.get_all() if d.integration == self.name]
            for device in devices:
                if isinstance(device, ESPhomeDevice):
                    asyncio.create_task(self._connect_device(device))

    async def _connect_device(self, device: ESPhomeDevice) -> None:
        async with self._lock:
            if device.id in self._connections:
                return

            data = device.integration_data
            conn = ESPhomeDeviceConnection(
                device_id=device.id,
                address=data.address,
                port=data.port,
                encryption_key=data.encryption_key,
                on_state=self._on_state,
            )
            self._connections[device.id] = conn

        try:
            await conn.start()
            await conn.wait_ready()
            await self.dependencies.output.controller_did_connect_device(self, device.id)
            device.available = True
            device.last_error = None
            async with self.dependencies.make_device_repository() as repo:
                await repo.save(device)
        except Exception as exc:
            logger.error("Failed to connect to %s: %s", data.address, exc)
            device.available = False
            device.last_error = str(exc)
            async with self._lock:
                self._connections.pop(device.id, None)
            async with self.dependencies.make_device_repository() as repo:
                await repo.save(device)

    async def _disconnect_device(self, device_id: UUID) -> None:
        async with self._lock:
            conn = self._connections.pop(device_id, None)
            self._state_cache.pop(device_id, None)
        if conn:
            await conn.stop()


    async def _on_state(
        self,
        device_id: UUID,
        entity_name: str,
        component_type: str,
        state_obj: Any,
    ) -> None:
        try:
            sub_values = mapper.convert_entity_state(state_obj, component_type)
            events: list[DeviceParameterChangedEvent] = []

            cache = self._state_cache.setdefault(device_id, {})

            for sub_field, value in sub_values:
                param_id = uuid5(NAMESPACE_DNS,
                    f"{device_id}_{entity_name}_{sub_field}"
                )
                cache[f"{entity_name}_{sub_field}"] = value
                events.append(
                    DeviceParameterChangedEvent(
                        device_id=device_id,
                        parameter_id=param_id,
                        value=value,
                    )
                )

            if events:
                await self.dependencies.output.controller_did_receive_events(
                    self, events
                )
        except Exception:
            logger.exception("Error handling state for %s", entity_name)


    async def pair_device(
        self,
        discovery: Discovery,
        credentials: CredentialsValue | None,
    ) -> None:
        logger.info("Pairing ESPHome device: %s", discovery.id)

        encryption_key: Optional[str] = None
        if credentials and credentials.type == "encryption_key":
            encryption_key = credentials.value

        if discovery.integration_data.get("requires_encryption") and not encryption_key:
            raise ValueError("Encryption key is required for this device")

        integration_data = discovery.integration_data or {}
        address = integration_data.get("address")
        port = integration_data.get("port", 6053)

        conn = ESPhomeDeviceConnection(
            device_id=discovery.id,
            address=address,
            port=port,
            encryption_key=encryption_key,
            on_state=self._on_state,
        )

        try:
            await conn.start()
            await conn.wait_ready()

            entities = conn.get_entities()
            parameters: list[ESPhomeParameter] = []

            for entity_key, entity in entities.items():
                if not entity.name:
                    continue

                component_type_str = getattr(entity, "type", "unknown")
                try:
                    comp_enum = ESPhomeComponentType(component_type_str)
                except ValueError:
                    logger.warning("Unknown component type: %s", component_type_str)
                    continue

                sub_fields = mapper.get_sub_fields(comp_enum)

                if sub_fields:
                    for sub_field, data_type, role in sub_fields:
                        param_id = uuid5(NAMESPACE_DNS,
                            f"{discovery.id}_{entity.name}_{sub_field}"
                        )

                        enum_values = None
                        if comp_enum == ESPhomeComponentType.SELECT and sub_field == "state" and hasattr(entity, "options"):
                            enum_values = list(entity.options)
                        elif comp_enum == ESPhomeComponentType.CLIMATE and sub_field == "mode" and hasattr(entity, "modes"):
                            enum_values = list(entity.modes)

                        is_number_value = comp_enum == ESPhomeComponentType.NUMBER and sub_field == "state"

                        param = ESPhomeParameter(
                            id=param_id,
                            name=f"{entity.name}_{sub_field}",
                            data_type=data_type,
                            role=role,
                            unit=get_unit(component_type_str, getattr(entity, "device_class", None)),
                            min_value=get_min_value(component_type_str) if is_number_value else None,
                            max_value=get_max_value(component_type_str) if is_number_value else None,
                            step=get_min_step(component_type_str) if is_number_value else None,
                            enum_values=enum_values,
                            visibility=ParameterVisibility.user,
                            integration_data=ESPhomeParameterIntegrationData(
                                entity_name=entity.name,
                                component_type=comp_enum,
                                parameter_type=mapper.COMPONENT_TO_PARAMETER_TYPE.get(
                                    comp_enum, ESPhomeParameterType.STATE
                                ),
                                service_key=entity_key,
                                sub_field=sub_field,
                            ),
                        )
                        parameters.append(param)
                else:
                    param_id = uuid5(NAMESPACE_DNS,
                        f"{discovery.id}_{entity.name}_state"
                    )

                    enum_values = None
                    if comp_enum == ESPhomeComponentType.SELECT and hasattr(entity, "options"):
                        enum_values = list(entity.options)

                    param = ESPhomeParameter(
                        id=param_id,
                        name=entity.name,
                        data_type=mapper.COMPONENT_TO_DATATYPE.get(
                            comp_enum, ParameterDataType.none
                        ),
                        role=mapper.COMPONENT_TO_ROLE.get(
                            comp_enum, ParameterRole.control
                        ),
                        unit=get_unit(component_type_str, getattr(entity, "device_class", None)),
                        min_value=get_min_value(component_type_str),
                        max_value=get_max_value(component_type_str),
                        step=get_min_step(component_type_str),
                        enum_values=enum_values,
                        visibility=ParameterVisibility.user,
                        integration_data=ESPhomeParameterIntegrationData(
                            entity_name=entity.name,
                            component_type=comp_enum,
                            parameter_type=mapper.COMPONENT_TO_PARAMETER_TYPE.get(
                                comp_enum, ESPhomeParameterType.STATE
                            ),
                            service_key=entity_key,
                            sub_field="state",
                        ),
                    )
                    parameters.append(param)

            device = ESPhomeDevice(
                id=discovery.id,
                name=discovery.name,
                integration=self.name,
                available=True,
                parameters=parameters,
                room_id=discovery.id,   
                transport=getattr(discovery, 'transport', 'tcp'),
                manufacturer=getattr(discovery, 'device_manufacturer', 'esphome'),
                integration_data=ESPhomeDeviceIntegrationData(
                    device_name=discovery.name,
                    unique_id=str(discovery.id),
                    address=address,
                    port=port,
                    encryption_key=encryption_key,
                ),
            )

            async with self.dependencies.make_device_repository() as repo:
                await repo.save(device)

            async with self._lock:
                self._connections[discovery.id] = conn

            await self.dependencies.output.controller_did_connect_device(self, discovery.id)

            self._discoveries.pop(discovery.id, None)

        except Exception:
            await conn.stop()
            raise

    async def unpair(self, device: ESPhomeDevice) -> None:
        await self._disconnect_device(device.id)
        async with self.dependencies.make_device_repository() as repo:
            await repo.delete(device.id)

    async def identify(self, device: ESPhomeDevice) -> None:
        logger.info("Identify called for %s", device.id)

    async def fetch(self, device: ESPhomeDevice) -> None:
        conn = self._connections.get(device.id)
        if not conn:
            raise ConnectionError("Device not connected")
  

    async def send_command(
        self,
        command: DeviceCommand,
        device: ESPhomeDevice,
        parameter: ESPhomeParameter,
    ) -> None:
        conn = self._connections.get(device.id)
        if not conn:
            raise ConnectionError("Device not connected")

        entity_key = parameter.integration_data.service_key
        component_type = parameter.integration_data.component_type.value
        sub_field = parameter.integration_data.sub_field

        cache = self._state_cache.get(device.id, {})
        command_args = mapper.build_command_args(
            component_type=component_type,
            entity_key=entity_key,
            sub_field=sub_field,
            value=command.value,
            current_state=cache,
        )
        await conn.send_command(entity_key, component_type, command_args)