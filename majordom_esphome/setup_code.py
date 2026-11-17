"""Setup code authentication, as proposed for Improv via BLE revision 2.5 (improv-wifi/website#70, at dd868be): the Hub
proves it knows the 8-digit code printed on the device with SPAKE2+ (RFC 9383, P-256, the verifier derived as Matter
derives its own), and every command then travels sealed (AES-128-GCM) in an Encrypted RPC frame. The device keeps only
a verifier; the code never crosses the air. The `thread_provisioning` service speaks it too, with its own UUIDs.

`SetupCode` is the `SecureChannel` that does this on top of another one (LE Secure Connections, the link pairing).
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from majordom_integration_sdk.capabilities import BLEConnection, GattService, SecureChannel

logger = logging.getLogger(__name__)

# P-256 (SEC 2): field prime, group order, curve coefficient b (a = -3), generator
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B

type Point = tuple[int, int] | None  # None: the point at infinity

G: Point = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)

COMMAND_BEGIN = 0x08
COMMAND_SHARE = 0x09
COMMAND_CONFIRM = 0x0A
COMMAND_ENCRYPTED = 0x0B
CAPABILITY = 1 << 4
ERROR_AUTHENTICATION_FAILED = 0x06
ERROR_TOO_MANY_ATTEMPTS = 0x07
CONTEXT_PREFIX = b"Improv Wi-Fi PAKE v1"
FORBIDDEN_CODES = {int(str(d) * 8) for d in range(10)} | {12345678, 87654321}
RESULT_TIMEOUT = 10.0  # per handshake step
ATT_HEADER = 3  # bytes of a write request that are not its value

WRONG_CODE = "The setup code is wrong: check the code printed on the device, then pair it again"
TOO_MANY_ATTEMPTS = "Too many wrong setup codes: wait a minute, then pair the device again"
NO_SETUP_CODE = "The device has no setup code: pair it without one"
REFUSED = "The device refused the setup code handshake: pair it again"


class SetupCodeError(Exception):
    """The setup code handshake failed, so nothing was sent; the message is meant for the user."""


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    slope = (3 * x1 * x1 - 3) * pow(2 * y1, -1, P) if p1 == p2 else (y2 - y1) * pow(x2 - x1, -1, P)
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def mul(k: int, point: Point) -> Point:
    """Double-and-add, not constant time: the Hub's scalars are ephemeral or derived from a code it was handed."""
    result: Point = None
    for bit in bin(k % ORDER)[2:]:
        result = _add(result, result)
        if bit == "1":
            result = _add(result, point)
    return result


def encode(point: Point) -> bytes:
    if point is None:
        raise ValueError("The point at infinity has no encoding")
    return b"\x04" + point[0].to_bytes(32) + point[1].to_bytes(32)


def decode(data: bytes) -> Point:
    """An uncompressed SEC1 point, which must be on the curve (and so not the point at infinity)."""
    if len(data) != 65 or data[0] != 0x04:
        raise ValueError("Not an uncompressed P-256 point")
    x, y = int.from_bytes(data[1:33]), int.from_bytes(data[33:])
    if x >= P or y >= P or (y * y - x * x * x + 3 * x - B) % P:
        raise ValueError("Not a point on P-256")
    return x, y


M = decode(
    bytes.fromhex(
        "04886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"
        "5ff355163e43ce224e0b0e65ff02ac8e5c7be09419c785e0ca547d55a12e2d20"
    )
)
N = decode(
    bytes.fromhex(
        "04d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"
        "07d60aa6bfade45008a636337f5168c64d9bd36034808cd564490b1e656edbe7"
    )
)


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA-256 with an empty salt (RFC 5869)."""
    prk = hmac.digest(bytes(32), ikm, "sha256")
    okm, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.digest(prk, block + info + bytes([counter]), "sha256")
        okm += block
    return okm[:length]


@dataclass(frozen=True)
class Keys:
    confirm_p: bytes  # the prover's confirmation, HMAC(K_confirmP, shareV)
    confirm_v: bytes  # the verifier's, HMAC(K_confirmV, shareP)
    shared: bytes  # K_shared


def key_schedule(context: bytes, id_p: bytes, id_v: bytes, *values: bytes) -> Keys:
    """RFC 9383 section 3.4; ``values``: shareP, shareV, Z, V, w0, in this order."""
    transcript = b"".join(
        len(item).to_bytes(8, "little") + item for item in (context, id_p, id_v, encode(M), encode(N), *values)
    )
    main = hashlib.sha256(transcript).digest()
    confirmation = _hkdf(main, b"ConfirmationKeys", 64)
    share_p, share_v = values[0], values[1]
    return Keys(
        confirm_p=hmac.digest(confirmation[:32], share_v, "sha256"),
        confirm_v=hmac.digest(confirmation[32:], share_p, "sha256"),
        shared=_hkdf(main, b"SharedKey", 32),
    )


class Prover:
    """The client's side of SPAKE2+: ``share`` goes to the verifier, whose share ``finish`` turns into the keys."""

    def __init__(self, w0: int, w1: int, context: bytes, id_p: bytes = b"", id_v: bytes = b"", x: int | None = None):
        self._w0, self._w1, self._ids = w0, w1, (context, id_p, id_v)
        self._x = x or secrets.randbelow(ORDER - 1) + 1
        self.share = encode(_add(mul(self._x, G), mul(w0, M)))

    def finish(self, share_v: bytes) -> Keys:
        """Raises `ValueError` for a share that is not a valid point."""
        unblinded = _add(decode(share_v), mul(ORDER - self._w0, N))
        z, v = encode(mul(self._x, unblinded)), encode(mul(self._w1, unblinded))
        return key_schedule(*self._ids, self.share, share_v, z, v, self._w0.to_bytes(32))


def parse_code(code: str) -> int:
    """The 8-digit setup code, dashes and spaces allowed; raises `ValueError` (meant for the user) for anything else."""
    digits = code.replace("-", "").replace(" ", "")
    if len(digits) != 8 or not digits.isdecimal() or int(digits) in FORBIDDEN_CODES:
        raise ValueError("The setup code is the 8 digits printed on the device")
    return int(digits)


def derive(code: int, salt: bytes, iterations: int) -> tuple[int, int]:
    """w0, w1 from the setup code, as Matter derives them (the code as a 4-byte little-endian password)."""
    output = hashlib.pbkdf2_hmac("sha256", code.to_bytes(4, "little"), salt, iterations, 80)
    return int.from_bytes(output[:40]) % ORDER, int.from_bytes(output[40:]) % ORDER


def context(salt: bytes, iterations: int) -> bytes:
    return hashlib.sha256(CONTEXT_PREFIX + salt + iterations.to_bytes(4, "little")).digest()


def frame(command: int, data: bytes) -> bytes:
    """Improv's frame: command, data length, data, then the sum of the previous bytes."""
    body = bytes([command, len(data)]) + data
    return body + bytes([sum(body) & 0xFF])


def entries(result: bytes) -> list[bytes]:
    """The entries of a result frame (command, length, then length-prefixed entries, checksum)."""
    if len(result) < 3 or result[1] != len(result) - 3 or sum(result[:-1]) & 0xFF != result[-1]:
        raise ValueError("Malformed result frame")
    items, data = [], result[2:-1]
    while data:
        size = data[0]
        if len(data) < size + 1:
            raise ValueError("Malformed result entry")
        items.append(data[1 : size + 1])
        data = data[size + 1 :]
    return items


class Session:
    """The keys of one handshake: client-to-device and device-to-client AES-128-GCM, each with its message counter."""

    def __init__(self, shared: bytes):
        self._outgoing, self._incoming = AESGCM(shared[:16]), AESGCM(shared[16:])
        self._sent, self._received = 0, -1

    def seal(self, plaintext: bytes) -> bytes:
        """The Encrypted RPC data for an inner command (command, length, data): counter, ciphertext, tag."""
        if self._sent > 0xFFFFFFFF:
            raise SetupCodeError(REFUSED)  # counters never wrap: a new handshake is needed
        counter = self._sent.to_bytes(4, "little")
        self._sent += 1
        return counter + self._outgoing.encrypt(counter + bytes(8), plaintext, bytes([COMMAND_ENCRYPTED, 0x00]))

    def open(self, sealed: bytes) -> bytes:
        """The inner result of an Encrypted RPC result entry; raises `ValueError` for a replayed or forged one."""
        if len(sealed) < 20 or (counter := int.from_bytes(sealed[:4], "little")) <= self._received:
            raise ValueError("Replayed or truncated packet")
        try:
            plaintext = self._incoming.decrypt(sealed[:4] + bytes(8), sealed[4:], bytes([COMMAND_ENCRYPTED, 0x01]))
        except InvalidTag as exc:
            raise ValueError("Forged packet") from exc
        self._received = counter
        return plaintext


@dataclass(frozen=True)
class RpcService:
    """An Improv-style GATT service: where commands go and where results, errors and capabilities are read."""

    service: str
    rpc: str
    result: str
    error: str
    capabilities: str


class SetupCode:
    """A `SecureChannel`: proves the setup code to the device over ``base`` (LE Secure Connections, usually), then
    yields a connection that seals every command it writes and opens every result. Fails closed: a device without
    setup code support, a wrong code or a refused handshake raise `SetupCodeError` and nothing is written."""

    def __init__(self, code: str, service: RpcService, base: SecureChannel):
        self._code, self._service, self._base = parse_code(code), service, base
        self.is_pairing_needed = base.is_pairing_needed

    @contextlib.asynccontextmanager
    async def secure(self, connection: BLEConnection) -> AsyncGenerator[BLEConnection]:
        async with self._base.secure(connection) as link:
            sealed = await SealedConnection.open(link, self._service)
            await sealed.authenticate(self._code)
            yield sealed


async def write_chunks(connection: BLEConnection, handle: int, data: bytes):
    size = connection.mtu - ATT_HEADER
    for start in range(0, len(data), size):
        await connection.write(handle, data[start : start + size], is_response_needed=True)


class SealedConnection:
    """A `BLEConnection` over an authenticated session: commands written to the RPC characteristic leave sealed in an
    Encrypted RPC frame, Encrypted RPC results come back opened. Result and error notifications are subscribed once, for
    the handshake, and handed on to whoever subscribes afterwards."""

    def __init__(self, link: BLEConnection, handles: dict[str, int], service: RpcService):
        self._link, self._handles, self._service = link, handles, service
        self._listeners: dict[int, Callable[[bytes], None]] = {}
        self._session: Session | None = None
        self._outgoing = b""

    @classmethod
    async def open(cls, link: BLEConnection, service: RpcService) -> Self:
        (gatt,) = (s for s in await link.services() if s.uuid.lower() == service.service)
        sealed = cls(link, {c.uuid.lower(): c.handle for c in gatt.characteristics}, service)
        for uuid in (service.result, service.error):
            handle = sealed._handles[uuid]
            await link.start_notify(handle, lambda data, handle=handle: sealed._dispatch(handle, data))
        return sealed

    def _dispatch(self, handle: int, data: bytes):
        if handle == self._handles[self._service.result] and self._session and data[:1] == bytes([COMMAND_ENCRYPTED]):
            try:
                (sealed,) = entries(data)
                inner = self._session.open(sealed)
            except ValueError as exc:
                logger.warning("Dropped a result that did not open: %s", exc)
                return
            data = inner + bytes([sum(inner) & 0xFF])
        if listener := self._listeners.get(handle):
            listener(data)

    async def authenticate(self, code: int):
        capabilities = self._handles.get(self._service.capabilities)
        if capabilities is None or not (await self._link.read(capabilities))[0] & CAPABILITY:
            raise SetupCodeError(NO_SETUP_CODE)
        events: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        for uuid in (self._service.result, self._service.error):
            self._listeners[self._handles[uuid]] = lambda data, uuid=uuid: events.put_nowait((uuid, data))
        try:
            salt, iterations = await self._call(COMMAND_BEGIN, b"", events)
            rounds = int.from_bytes(iterations, "little")
            if not 16 <= len(salt) <= 32 or len(iterations) != 4 or not 1000 <= rounds <= 100000:
                raise SetupCodeError(REFUSED)
            w0, w1 = await asyncio.to_thread(derive, code, salt, rounds)
            prover = Prover(w0, w1, context(salt, rounds))
            share_v, confirm_v = await self._call(COMMAND_SHARE, prover.share, events)
            try:
                keys = prover.finish(share_v)
            except ValueError:
                raise SetupCodeError(REFUSED) from None
            if not hmac.compare_digest(keys.confirm_v, confirm_v):
                raise SetupCodeError(WRONG_CODE)
            await self._call(COMMAND_CONFIRM, keys.confirm_p, events)
            self._session = Session(keys.shared)
        finally:
            self._listeners = {}

    async def _call(self, command: int, data: bytes, events: asyncio.Queue[tuple[str, bytes]]) -> list[bytes]:
        """Sends a handshake command; its result's entries, checked for count (and the share's size)."""
        await write_chunks(self._link, self._handles[self._service.rpc], frame(command, data))
        try:
            async with asyncio.timeout(RESULT_TIMEOUT):
                while True:
                    uuid, value = await events.get()
                    if uuid == self._service.error and value[:1] == bytes([ERROR_TOO_MANY_ATTEMPTS]):
                        raise SetupCodeError(TOO_MANY_ATTEMPTS)
                    if uuid == self._service.error and value[:1] == bytes([ERROR_AUTHENTICATION_FAILED]):
                        raise SetupCodeError(WRONG_CODE if command == COMMAND_CONFIRM else REFUSED)
                    if uuid == self._service.result and value[:1] == bytes([command]):
                        items = entries(value)
                        break
        except (TimeoutError, ValueError):
            raise SetupCodeError(REFUSED) from None
        if len(items) != (0 if command == COMMAND_CONFIRM else 2) or (command == COMMAND_SHARE and len(items[0]) != 65):
            raise SetupCodeError(REFUSED)
        return items

    # BLEConnection

    @property
    def mtu(self) -> int:
        return self._link.mtu

    async def services(self) -> list[GattService]:
        return await self._link.services()

    async def read(self, handle: int) -> bytes:
        return await self._link.read(handle)

    async def write(self, handle: int, data: bytes, is_response_needed: bool = True):
        if handle != self._handles[self._service.rpc] or self._session is None:
            await self._link.write(handle, data, is_response_needed)
            return
        self._outgoing += data  # a command may come in chunks: it is sealed whole
        if len(self._outgoing) < 3 or len(self._outgoing) < self._outgoing[1] + 3:
            return
        command, self._outgoing = self._outgoing, b""
        if len(command) != command[1] + 3 or sum(command[:-1]) & 0xFF != command[-1]:
            raise ValueError("Malformed command frame")
        await write_chunks(self._link, handle, frame(COMMAND_ENCRYPTED, self._session.seal(command[:-1])))

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        if handle in (self._handles[self._service.result], self._handles[self._service.error]):
            self._listeners[handle] = callback
        else:
            await self._link.start_notify(handle, callback)

    async def stop_notify(self, handle: int):
        if self._listeners.pop(handle, None) is None:
            await self._link.stop_notify(handle)

    async def disconnect(self):
        await self._link.disconnect()
