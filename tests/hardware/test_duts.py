"""The cage guard refuses any device but the DUT, heard as strongly as from inside the closed cage."""

import pytest

from tests.hardware.duts import ESPHOME_C3, Dut, pick

THE_C3 = {"mac": "7c4fadd17b00", "name": "Cage C3"}


def test_the_dut_heard_at_its_threshold_is_picked():
    assert pick(ESPHOME_C3, [THE_C3], identity=lambda c: c, rssi=lambda _: -60) is THE_C3


@pytest.mark.parametrize(
    ("candidate", "level"),
    [
        ({"mac": "7c4fadd17b01", "name": "Cage C3"}, -40),  # another C3, named alike
        ({"mac": "7c4fadd17b00", "name": "md-hw-improv"}, -40),  # the MAC alone is not enough
        (THE_C3, -61),  # too weak: the cage is open, or it is not inside
        (THE_C3, None),  # no signal reported yet
    ],
)
def test_anything_else_is_refused(candidate: dict[str, str], level: int | None):
    with pytest.raises(AssertionError, match="refusing to act"):
        pick(ESPHOME_C3, [candidate], identity=lambda c: c, rssi=lambda _: level)


def test_nothing_heard_is_refused():
    with pytest.raises(AssertionError, match="refusing to act"):
        pick(ESPHOME_C3, [], identity=lambda c: c, rssi=lambda _: -30)


def test_the_strongest_match_is_picked():
    near, far = dict(THE_C3), dict(THE_C3)
    levels = {id(near): -30, id(far): -55}
    assert pick(ESPHOME_C3, [far, near], identity=lambda c: c, rssi=lambda c: levels[id(c)]) is near


def test_the_threshold_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAGE_MIN_RSSI_ESPHOME", "-50")
    assert Dut("esphome", "Cage C3", None, THE_C3, -60).threshold == -50
