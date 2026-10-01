"""mDNS discovery: nodes announced through the SDK's zeroconf service become correct discoveries."""

import pytest
from majordom_integration_sdk.schemas import CredentialsType
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from tests.virtual.mdns import Advert, FakeMDNS, settle

MAC = "983569abf679"


async def test_announced_node_becomes_a_discovery(
    controller: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput
):
    await mdns.announce(Advert())

    assert len(output.received_discoveries) == 1
    discovered = output.received_discoveries[0]
    assert controller.discoveries == {discovered.id: discovered}
    assert discovered.integration == controller.name


async def test_controller_stops_listening_on_stop(controller: ESPhomeController, mdns: FakeMDNS):
    await controller.stop()
    await settle()  # the SDK cancels the browser in a task

    await mdns.announce(Advert())

    assert controller.discoveries == {}


async def test_discovery_id_is_the_sdk_hardware_identity(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert(mac=MAC))

    assert list(controller.discoveries) == [controller.device_uuid(MAC)]


async def test_discovery_is_named_after_the_node_not_its_mdns_record(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert(name="kitchen_relay"))

    assert next(iter(controller.discoveries.values())).device_name == "kitchen_relay"


async def test_friendly_name_is_preferred_when_the_node_has_one(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert(name="kitchen_relay", friendly_name="Kitchen Relay"))

    assert next(iter(controller.discoveries.values())).device_name == "Kitchen Relay"


async def test_discovery_does_not_claim_a_device_category(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert())

    assert next(iter(controller.discoveries.values())).device_category is None  # a node may host anything


async def test_plain_node_needs_no_credentials(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert(encrypted=False))

    assert next(iter(controller.discoveries.values())).expected_credentials_options == [CredentialsType.none]


async def test_encrypted_node_asks_for_its_key(controller: ESPhomeController, mdns: FakeMDNS):
    await mdns.announce(Advert(encrypted=True))

    assert next(iter(controller.discoveries.values())).expected_credentials_options == [CredentialsType.secret]


async def test_repeated_announcement_is_not_a_new_discovery(
    controller: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput
):
    await mdns.announce(Advert())
    await mdns.announce(Advert())

    assert len(output.received_discoveries) == 1
    assert output.updated_discoveries == []


async def test_changed_announcement_updates_the_discovery(
    controller: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput
):
    await mdns.announce(Advert(encrypted=False))

    await mdns.announce(Advert(encrypted=True))

    assert len(output.updated_discoveries) == 1
    assert output.updated_discoveries[0].expected_credentials_options == [CredentialsType.secret]
    assert list(controller.discoveries.values()) == output.updated_discoveries


async def test_vanished_node_is_lost(controller: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput):
    advert = Advert()
    await mdns.announce(advert)
    (discovery_id,) = controller.discoveries

    await mdns.goodbye(advert)

    assert output.lost_discoveries == [discovery_id]
    assert controller.discoveries == {}


@pytest.mark.parametrize(
    "incomplete", [Advert(mac=None), Advert(server=None, addresses=[])], ids=["no-mac", "no-address"]
)
async def test_incomplete_announcement_never_raises(
    controller: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput, incomplete: Advert
):
    await mdns.announce(incomplete)  # must not break the SDK's discovery loop

    assert output.errors == []
