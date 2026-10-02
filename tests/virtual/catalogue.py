"""The catalogue: one virtual node per entity kind ESPHome's API knows, generated, validated and built from recipes.

Modelled on the Matter integration's virtual-device catalogue: the list of kinds comes from the installed
`aioesphomeapi`, so a library upgrade that adds a kind is noticed (`test_catalogue.py`) and, unless the kind has a
recipe, tried with a guessed `template` platform (`python -m tests.virtual.catalogue`). Kinds that cannot run on the
host node are listed in `UNHOSTABLE` with the reason.

    python -m tests.virtual.catalogue            # generate, validate and build every node (in parallel)
    python -m tests.virtual.catalogue generate   # only write the yaml files
"""

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aioesphomeapi import COMPONENT_TYPE_TO_INFO

from tests.virtual.runner import Sketch, build

NODES_DIR = Path(__file__).parent / "nodes"  # generated, git-ignored
API_PORT = 6060  # one node runs at a time

HEADER = """\
esphome:
  name: {name}

host:
  mac_address: "{mac}"

api:
  port: {port}

logger:

"""

# The entity of each kind, as the body of a node. Most use the `template` platform with optimistic writes, so a
# command is visible as a state change.
RECIPES: dict[str, str] = {
    "binary_sensor": """\
binary_sensor:
  - platform: template
    name: "Probe"
    lambda: "return true;"
""",
    "sensor": """\
sensor:
  - platform: template
    name: "Probe"
    lambda: "return 21.5;"
    update_interval: 1s
""",
    "text_sensor": """\
text_sensor:
  - platform: template
    name: "Probe"
    lambda: 'return {"OK"};'
    update_interval: 1s
""",
    "switch": """\
switch:
  - platform: template
    name: "Probe"
    optimistic: true
""",
    "button": """\
button:
  - platform: template
    name: "Probe"
    on_press:
      - logger.log: "pressed"
""",
    "number": """\
number:
  - platform: template
    name: "Probe"
    min_value: 10
    max_value: 30
    step: 0.5
    optimistic: true
""",
    "select": """\
select:
  - platform: template
    name: "Probe"
    options: ["A", "B", "C"]
    optimistic: true
""",
    "text": """\
text:
  - platform: template
    name: "Probe"
    optimistic: true
    min_length: 0
    max_length: 20
    mode: text
""",
    "lock": """\
lock:
  - platform: template
    name: "Probe"
    optimistic: true
    lock_action:
      - logger.log: "lock"
    unlock_action:
      - logger.log: "unlock"
    open_action:
      - logger.log: "open"
""",
    "valve": """\
valve:
  - platform: template
    name: "Probe"
    optimistic: true
    has_position: true
    open_action:
      - logger.log: "open"
    close_action:
      - logger.log: "close"
    stop_action:
      - logger.log: "stop"
    position_action:
      - logger.log: "position"
""",
    "cover": """\
cover:
  - platform: template
    name: "Probe"
    optimistic: true
    has_position: true
    open_action:
      - logger.log: "open"
    close_action:
      - logger.log: "close"
    stop_action:
      - logger.log: "stop"
    position_action:
      - logger.log: "position"
""",
    "fan": """\
fan:
  - platform: template
    name: "Probe"
    speed_count: 3
    has_direction: true
    has_oscillating: true
""",
    "light": """\
output:
  - platform: template
    id: out_r
    type: float
    write_action:
      - logger.log: "r"
  - platform: template
    id: out_g
    type: float
    write_action:
      - logger.log: "g"
  - platform: template
    id: out_b
    type: float
    write_action:
      - logger.log: "b"

light:
  - platform: rgb
    name: "Probe"
    red: out_r
    green: out_g
    blue: out_b
""",
    "climate": """\
sensor:
  - platform: template
    id: room
    lambda: "return 21.5;"
    update_interval: 1s

climate:
  - platform: thermostat
    name: "Probe"
    sensor: room
    default_preset: Default
    preset:
      - name: Default
        default_target_temperature_low: 20
        default_target_temperature_high: 24
    heat_action:
      - logger.log: "heat"
    cool_action:
      - logger.log: "cool"
    idle_action:
      - logger.log: "idle"
    min_idle_time: 1s
    min_heating_off_time: 1s
    min_heating_run_time: 1s
    min_cooling_off_time: 1s
    min_cooling_run_time: 1s
""",
    "date": """\
datetime:
  - platform: template
    name: "Probe"
    type: date
    optimistic: true
""",
    "time": """\
datetime:
  - platform: template
    name: "Probe"
    type: time
    optimistic: true
""",
    "datetime": """\
datetime:
  - platform: template
    name: "Probe"
    type: datetime
    optimistic: true
""",
    "alarm_control_panel": """\
alarm_control_panel:
  - platform: template
    name: "Probe"
    codes: ["1234"]
    requires_code_to_arm: false
    arming_home_time: 1s
    arming_away_time: 1s
    pending_time: 1s
    trigger_time: 1s
""",
    "event": """\
event:
  - platform: template
    name: "Probe"
    event_types:
      - "ring"
""",
    "water_heater": """\
water_heater:
  - platform: template
    name: "Probe"
""",
}

# Kinds the host node cannot run, and why. A kind the library knows that is in neither table is a new kind.
UNHOSTABLE: dict[str, str] = {
    "siren": "ESPHome has no siren component",
    "radio_frequency": "ESPHome has no radio_frequency component",
    "camera": "needs camera hardware (esp32_camera)",
    "media_player": "needs audio hardware (speaker, i2s_audio)",
    "infrared": "no infrared platform runs on the host",
    "update": "the only platforms (http_request, esp32_hosted) need a network stack or hardware the host lacks",
}


def all_kinds() -> list[str]:
    """Every entity kind the installed `aioesphomeapi` knows."""
    return list(COMPONENT_TYPE_TO_INFO)


def guessed_recipe(kind: str) -> str:
    """A new kind without a recipe: try the `template` platform with nothing but a name."""
    return f'{kind}:\n  - platform: template\n    name: "Probe"\n'


def sketch_for(kind: str) -> Sketch:
    index = all_kinds().index(kind)
    name = f"cat_{kind}"
    return Sketch(f"{name}.yaml", name, API_PORT, f"983569{index:06x}", directory=NODES_DIR)


def generate(kinds: list[str] | None = None) -> list[str]:
    """Write the node yaml of every hostable kind; returns the kinds written (guessed recipes included)."""
    NODES_DIR.mkdir(exist_ok=True)
    written = []
    for kind in kinds or all_kinds():
        if kind in UNHOSTABLE:
            continue
        sketch = sketch_for(kind)
        body = RECIPES.get(kind) or guessed_recipe(kind)
        (NODES_DIR / sketch.yaml).write_text(
            HEADER.format(name=sketch.name, mac=_colon(sketch.mac), port=API_PORT) + body
        )
        written.append(kind)
    return written


def _colon(mac: str) -> str:
    return ":".join(mac[i : i + 2] for i in range(0, 12, 2))


def valid(kind: str) -> bool:
    """Whether esphome accepts the node (used to try guessed recipes without failing the whole build)."""
    sketch = sketch_for(kind)
    result = subprocess.run(["esphome", "config", str(NODES_DIR / sketch.yaml)], capture_output=True, text=True)
    return result.returncode == 0


def hosted_kinds() -> list[str]:
    """The kinds with a generated, built node: what the sweep runs over (no recipe needed for a new kind)."""
    if not NODES_DIR.exists():
        return []
    return [kind for kind in all_kinds() if sketch_for(kind).binary.exists()]


def build_all(kinds: list[str] | None = None, workers: int = 3) -> dict[str, str | None]:
    """Generate, validate and build the nodes in parallel; returns kind -> error (None when built)."""
    if shutil.which("esphome") is None:
        raise RuntimeError("`esphome` CLI not found: install it (e.g. `pipx install esphome`)")
    todo = generate(kinds)

    def one(kind: str) -> tuple[str, str | None]:
        sketch = sketch_for(kind)
        if sketch.binary.exists():
            return kind, None
        if kind not in RECIPES and not valid(kind):
            return kind, "guessed template recipe is not valid: add a recipe or list the kind in UNHOSTABLE"
        try:
            build(sketch)
        except subprocess.CalledProcessError as exc:
            return kind, f"esphome compile failed ({exc.returncode})"
        return kind, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, todo))


if __name__ == "__main__":
    if sys.argv[1:] == ["generate"]:
        print("generated:", ", ".join(generate()))
    else:
        results = build_all()
        for kind, error in results.items():
            print(f"{'ok  ' if error is None else 'FAIL'} {kind}" + (f": {error}" if error else ""))
        sys.exit(any(results.values()))
