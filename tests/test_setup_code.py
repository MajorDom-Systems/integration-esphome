"""Setup code authentication (Improv via BLE revision 2.5, improv-wifi/website#70): the SPAKE2+ core against RFC 9383's
P-256 test vector, the Improv profile against the RFC's complete vector (the firmware's verifier checks the same one),
and the channel against a scripted device: nothing is written unless the handshake confirms the code."""

import asyncio
import base64
from collections.abc import Callable

import pytest
from majordom_integration_sdk.capabilities import GattCharacteristic, GattService, Unsecured

from majordom_esphome import setup_code
from majordom_esphome.setup_code import (
    COMMAND_ENCRYPTED,
    G,
    Prover,
    RpcService,
    Session,
    SetupCode,
    SetupCodeError,
    context,
    decode,
    derive,
    encode,
    entries,
    frame,
    mul,
    parse_code,
)
from tests.helpers import FakeVerifier

h = bytes.fromhex

# RFC 9383 appendix C, SPAKE2+-P256-SHA256-HKDF-SHA256-HMAC-SHA256
RFC9383_CONTEXT = b"SPAKE2+-P256-SHA256-HKDF-SHA256-HMAC-SHA256 Test Vectors"
RFC9383_W0 = 0xBB8E1BBCF3C48F62C08DB243652AE55D3E5586053FCA77102994F23AD95491B3
RFC9383_W1 = 0x7E945F34D78785B8A3EF44D0DF5A1A97D6B3B460409A345CA7830387A74B1DBA
RFC9383_X = 0xD1232C8E8693D02368976C174E2088851B8365D0D79A9EEE709C6A05A2FAD539
RFC9383_SHARE_P = h(
    "04ef3bd051bf78a2234ec0df197f7828060fe9856503579bb1733009042c15c0c1de127727f418b5966afadfdd95a6e4591d171056b333dab97a"
    "79c7193e341727"
)
RFC9383_SHARE_V = h(
    "04c0f65da0d11927bdf5d560c69e1d7d939a05b0e88291887d679fcadea75810fb5cc1ca7494db39e82ff2f50665255d76173e09986ab46742c7"
    "98a9a68437b048"
)
RFC9383_CONFIRM_P = h("926cc713504b9b4d76c9162ded04b5493e89109f6d89462cd33adc46fda27527")
RFC9383_CONFIRM_V = h("9747bcc4f8fe9f63defee53ac9b07876d907d55047e6ff2def2e7529089d3e68")
RFC9383_K_SHARED = h("0c5f8ccd1413423a54f6c1fb26ff01534a87f893779c6e68666d772bfd91f3e7")

# improv-wifi/website#70 (dd868be), "Test vector"
SALT = FakeVerifier.SALT
SHARE_P = h(
    "044d0a606fb22bdb5f40f23a95bcd9b29346df87e0c4e18861366436d25d1c4e94963e021b443db17551776ad40f187ffcf89628f0bd52aafed2"
    "b271bad5d36398"
)
SHARE_V = h(
    "040beae881fcc4f61d79db4e17948af82e83c5e72b829081e18783ad61877a53b082de3b1eee9bfd121aa865f38d9eeb590cd54762131b3361a5"
    "3cc2e7da2e9bf4"
)
CONFIRM_P = h("f54ad1b72b094316e3c749548fe7de4f0c226e326727c5dc7f70ec061fd9565b")
CONFIRM_V = h("419fef16b131f42390a5cc5963f55542636d4636dc4b5651e39b7d010325253e")
K_SHARED = h("2c1792a85a9d6559a331c8335c01fa778506cd519fcb162d26dae3c2a0b97164")
WIFI_COMMAND = h("011e0c4d79576972656c6573734150106d7973656375726570617373776f7264")
WIFI_RESULT = h("011413687474703a2f2f3139322e3136382e312e3130")


def test_the_spake2plus_core_reproduces_rfc_9383():
    prover = Prover(RFC9383_W0, RFC9383_W1, RFC9383_CONTEXT, b"client", b"server", x=RFC9383_X)
    assert prover.share == RFC9383_SHARE_P

    keys = prover.finish(RFC9383_SHARE_V)

    assert (keys.confirm_p, keys.confirm_v, keys.shared) == (RFC9383_CONFIRM_P, RFC9383_CONFIRM_V, RFC9383_K_SHARED)


def test_the_verifier_is_matters_test_verifier():
    w0, w1 = derive(20202021, SALT, 1000)
    verifier = w0.to_bytes(32) + encode(mul(w1, G))

    assert base64.b64encode(verifier).decode() == (
        "uWFwqugDNGiEck/po7KHwwMwwqZgN10XuyBajPGuyzUEV/iree4lOrao5GuwnlQ65CJzbeUB49s31EH+NEkg0JVI5MGCQGMMT/SRPFNRODm3wH/M"
        "BiehuFc6FJ/NH6Rmzw=="
    )
    assert context(SALT, 1000) == h("705ff60b4c7df75fdf54705e37ad04051fd56106ecf579e9b451e51dcabc2447")


def test_the_handshake_reproduces_the_rfc_vector_packet_by_packet():
    w0, w1 = derive(20202021, SALT, 1000)
    prover = Prover(w0, w1, context(SALT, 1000), x=int("11" * 32, 16))
    keys = prover.finish(SHARE_V)

    assert (prover.share, keys.confirm_p, keys.confirm_v, keys.shared) == (SHARE_P, CONFIRM_P, CONFIRM_V, K_SHARED)
    assert frame(0x08, b"") == h("080008")
    assert entries(h("081610") + SALT + h("04e803000010")) == [SALT, h("e8030000")]
    assert frame(0x09, SHARE_P)[-1] == 0x6C
    assert entries(bytes([0x09, 0x63, 0x41]) + SHARE_V + bytes([0x20]) + CONFIRM_V + h("f8")) == [SHARE_V, CONFIRM_V]
    assert frame(0x0A, CONFIRM_P)[-1] == 0xEF
    assert entries(h("0a000a")) == []


def test_the_session_reproduces_the_rfc_vector_and_refuses_replays_and_forgeries():
    session = Session(K_SHARED)

    assert frame(COMMAND_ENCRYPTED, session.seal(WIFI_COMMAND)) == h(
        "0b34000000003e74e15b97c8434bdb335e660f01f5da0caea34630586fdb20e245e9457703365d6dcbfc01c1b2fc285712a58ee227cf02"
    )
    result = h("0b2b2a00000000f4348f8cc0f9bce72cfe63b874e74529f099a9b5bdc64d9c2900c4cf23119100c2e2a144314ee9")
    (sealed,) = entries(result)
    assert session.open(sealed) == WIFI_RESULT
    with pytest.raises(ValueError, match="Replayed"):
        session.open(sealed)
    forged = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(ValueError, match="Forged"):
        Session(K_SHARED).open(forged)


@pytest.mark.parametrize("code", ["2020-2021", "20202021", " 2020 2021 "])
def test_a_setup_code_may_be_grouped(code):
    assert parse_code(code) == 20202021


@pytest.mark.parametrize("code", ["", "1234567", "123456789", "abcd-efgh", "1111-1111", "1234-5678", "8765-4321"])
def test_a_malformed_or_forbidden_setup_code_is_refused(code):
    with pytest.raises(ValueError, match="8 digits"):
        parse_code(code)


def test_a_share_off_the_curve_is_refused():
    with pytest.raises(ValueError, match="P-256"):
        decode(b"\x04" + bytes(64))
    with pytest.raises(ValueError, match="uncompressed"):
        decode(SHARE_V[:33])


SERVICE = RpcService("aeb5-service", "aeb5-rpc", "aeb5-result", "aeb5-error", "aeb5-capabilities")
HANDLES = {SERVICE.rpc: 3, SERVICE.result: 4, SERVICE.error: 2, SERVICE.capabilities: 5}


class FakeVerifierDevice(FakeVerifier):
    """A device that speaks the handshake over its RPC characteristic, then answers any Encrypted RPC with a URL."""

    def __init__(self, capable: bool = True, iterations: int = 1000, error_on_begin: int | None = None):
        super().__init__(iterations, error_on_begin)
        self.capable = capable
        self.received: list[bytes] = []  # inner commands it opened
        self.plain: list[bytes] = []  # every frame it got, as it crossed the air
        self._callbacks: dict[int, Callable[[bytes], None]] = {}
        self._buffer = b""

    @property
    def mtu(self) -> int:
        return 23

    async def services(self) -> list[GattService]:
        characteristics = tuple(GattCharacteristic(uuid, handle, ("read",)) for uuid, handle in HANDLES.items())
        return [GattService(SERVICE.service, 1, characteristics)]

    async def read(self, handle: int) -> bytes:
        assert handle == HANDLES[SERVICE.capabilities]
        return bytes([setup_code.CAPABILITY if self.capable else 0])

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        self._callbacks[handle] = callback

    async def stop_notify(self, handle: int):
        self._callbacks.pop(handle)

    async def disconnect(self):
        pass

    async def write(self, handle: int, data: bytes, is_response_needed: bool = True):
        assert handle == HANDLES[SERVICE.rpc] and len(data) <= self.mtu - 3
        self._buffer += data
        if len(self._buffer) < 3 or len(self._buffer) < self._buffer[1] + 3:
            return
        command, self._buffer = self._buffer, b""
        self.plain.append(command)
        result, error, inner = self.answer(command[0], command[2:-1])
        if inner is not None:
            self.received.append(inner)
            result = self.seal(WIFI_RESULT)
        loop = asyncio.get_running_loop()
        if error is not None:
            loop.call_soon(self._callbacks[HANDLES[SERVICE.error]], bytes([error]))
        if result is not None:
            loop.call_soon(self._callbacks[HANDLES[SERVICE.result]], result)


async def test_commands_reach_the_device_sealed_once_the_code_is_proven():
    device = FakeVerifierDevice()
    results: list[bytes] = []

    async with SetupCode("2020-2021", SERVICE, Unsecured()).secure(device) as connection:
        await connection.start_notify(HANDLES[SERVICE.result], results.append)
        await connection.write(HANDLES[SERVICE.rpc], WIFI_COMMAND + bytes([sum(WIFI_COMMAND) & 0xFF]))
        await asyncio.sleep(0.01)

    assert device.received == [WIFI_COMMAND]
    assert results == [WIFI_RESULT + bytes([sum(WIFI_RESULT) & 0xFF])]
    assert not any(b"mysecurepassword" in sent for sent in device.plain), "the password crossed the air in clear text"
    assert all(sent[0] in (0x08, 0x09, 0x0A, COMMAND_ENCRYPTED) for sent in device.plain)


@pytest.mark.parametrize(
    ("device", "code", "message"),
    [
        (FakeVerifierDevice(), "2020-2022", "setup code is wrong"),
        (FakeVerifierDevice(capable=False), "2020-2021", "no setup code"),
        (FakeVerifierDevice(error_on_begin=setup_code.ERROR_TOO_MANY_ATTEMPTS), "2020-2021", "Too many"),
        (FakeVerifierDevice(iterations=999), "2020-2021", "refused"),
    ],
)
async def test_a_failed_handshake_yields_nothing(device, code, message):
    with pytest.raises(SetupCodeError, match=message):
        async with SetupCode(code, SERVICE, Unsecured()).secure(device):
            pytest.fail("credentials would be written without a confirmed setup code")
    assert device.received == []
    assert all(sent[0] in (0x08, 0x09) for sent in device.plain), "the client confirmed a code it could not verify"
