"""The cage's ESPHome device under test, and the guard a hardware test acts on it through.

Mirrored from the Hub's cage allowlist (MajorDom-Hub, tests/hardware/iot_cage/duts.py, branch feature/radio-transports):
keep `Dut`, `ESPHOME_C3` and `pick` in sync with it. Copied rather than imported, as the integration does not depend on
the Hub. A test may command only the DUT, identified by what it reports about itself and heard as strongly as from
inside the closed cage: with the cage open a neighbour's node can be in range and look alike, so the guard never falls
back to "the first device seen". No match, no action. The threshold can be overridden with CAGE_MIN_RSSI_ESPHOME.
"""

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Dut:
    key: str
    name: str
    slot: int | None  # its cage relay, None when the cage doesn't power it
    identity: dict[str, Any]  # what it must report about itself, every field equal
    min_rssi: int  # dBm

    @property
    def threshold(self) -> int:
        return int(os.environ.get(f"CAGE_MIN_RSSI_{self.key.upper()}", self.min_rssi))


ESPHOME_C3 = Dut("esphome", "Cage C3", None, {"mac": "7c4fadd17b00", "name": "Cage C3"}, -60)  # its Wi-Fi signal


def pick[T](
    dut: Dut, candidates: Iterable[T], identity: Callable[[T], dict[str, Any]], rssi: Callable[[T], int | None]
) -> T:
    """The strongest candidate that is ``dut`` and heard at its threshold or stronger. Fails, never guesses."""
    heard: list[tuple[int, T]] = []
    for candidate in candidates:
        reported, level = identity(candidate), rssi(candidate)
        match = all(reported.get(field) == value for field, value in dut.identity.items())
        verdict = "the DUT" if match else "not the DUT"
        log.info(f"[CAGE] {dut.name}? {reported} at {level} dBm: {verdict}")
        if match and level is not None and level >= dut.threshold:
            heard.append((level, candidate))
    assert heard, f"no {dut.name} heard at {dut.threshold} dBm or stronger: refusing to act on any other device"
    return max(heard, key=lambda found: found[0])[1]
