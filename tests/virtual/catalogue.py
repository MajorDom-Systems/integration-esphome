"""The catalogue: one virtual node per entity kind ESPHome's API knows, generated and built from recipes.

Modelled on the Matter integration's virtual-device catalogue. The list of kinds comes from the installed
`aioesphomeapi`, so a library upgrade that adds a kind is noticed (`test_catalogue.py`) and fails until someone writes
a recipe (or explains in `UNHOSTABLE` why the kind cannot run on the host node). A recipe is never guessed: a good one
switches on every capability of the entity (position, speed, colour modes, ...), and every field it exposes must be
accounted for in `EXPECTED` (mapped, with its type and role) or `UNMAPPED` (not mapped, with the reason), so the
more a recipe configures, the more of the mapping is tested.

    python -m tests.virtual.catalogue            # generate, validate and build every node (in parallel)
    python -m tests.virtual.catalogue generate   # only write the yaml files
"""

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aioesphomeapi import COMPONENT_TYPE_TO_INFO
from majordom_integration_sdk.schemas.parameter import ParameterDataType as T
from majordom_integration_sdk.schemas.parameter import ParameterRole as R

from tests.virtual.runner import Sketch

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
    tilt_action:
      - logger.log: "tilt"
    tilt_lambda: "return 0.5;"
""",
    "fan": """\
fan:
  - platform: template
    name: "Probe"
    speed_count: 3
    has_direction: true
    has_oscillating: true
    preset_modes: ["eco", "turbo"]
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
  - platform: template
    id: out_cw
    type: float
    write_action:
      - logger.log: "cw"
  - platform: template
    id: out_ww
    type: float
    write_action:
      - logger.log: "ww"

light:
  - platform: rgbww
    name: "Probe"
    red: out_r
    green: out_g
    blue: out_b
    cold_white: out_cw
    warm_white: out_ww
    cold_white_color_temperature: 153 mireds
    warm_white_color_temperature: 500 mireds
    effects:
      - random:
      - strobe:
""",
    "climate": """\
sensor:
  - platform: template
    id: room
    lambda: "return 21.5;"
    update_interval: 1s
  - platform: template
    id: room_humidity
    lambda: "return 40.0;"
    update_interval: 1s

climate:
  - platform: thermostat
    name: "Probe"
    sensor: room
    humidity_sensor: room_humidity
    default_preset: Home
    preset:
      - name: Home
        default_target_temperature_low: 20
        default_target_temperature_high: 24
      - name: Away
        default_target_temperature_low: 16
        default_target_temperature_high: 28
      - name: Night
        default_target_temperature_low: 17
        default_target_temperature_high: 19
    heat_action:
      - logger.log: "heat"
    cool_action:
      - logger.log: "cool"
    idle_action:
      - logger.log: "idle"
    fan_only_action:
      - logger.log: "fan only"
    humidity_control_humidify_action:
      - logger.log: "humidify"
    humidity_control_off_action:
      - logger.log: "humidity off"
    fan_mode_auto_action:
      - logger.log: "fan auto"
    fan_mode_on_action:
      - logger.log: "fan on"
    swing_both_action:
      - logger.log: "swing both"
    swing_off_action:
      - logger.log: "swing off"
    min_idle_time: 1s
    min_heating_off_time: 1s
    min_heating_run_time: 1s
    min_cooling_off_time: 1s
    min_cooling_run_time: 1s
    min_fanning_off_time: 1s
    min_fanning_run_time: 1s
    min_fan_mode_switching_time: 1s
""",
    "light/rgbw": """\
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
  - platform: template
    id: out_w
    type: float
    write_action:
      - logger.log: "w"

light:
  - platform: rgbw
    name: "Probe"
    red: out_r
    green: out_g
    blue: out_b
    white: out_w
""",
    "climate/single-point": """\
sensor:
  - platform: template
    id: room
    lambda: "return 21.5;"
    update_interval: 1s

climate:
  - platform: thermostat
    name: "Probe"
    sensor: room
    heat_action:
      - logger.log: "heat"
    idle_action:
      - logger.log: "idle"
    min_idle_time: 1s
    min_heating_off_time: 1s
    min_heating_run_time: 1s
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
    supported_modes: [ECO, ELECTRIC, PERFORMANCE]
    # a lambda per feature switches it on; `{}` keeps the value commands set (optimistic)
    current_temperature: "return 45.0;"
    target_temperature: "return {};"
    mode: "return {};"
    away: "return {};"
    is_on: "return {};"
""",
}

# Kinds the host node cannot run, and why (each reason was checked with `esphome config` / `esphome compile`)
UNHOSTABLE: dict[str, str] = {
    "siren": "ESPHome has no siren component: the kind exists in the API only",
    "radio_frequency": "needs ir_rf_proxy with a remote_transmitter, which has no host implementation (fails to link)",
    "infrared": "needs ir_rf_proxy with a remote_transmitter, which has no host implementation (fails to link)",
    "camera": "needs camera hardware: the only platform is esp32_camera",
    "media_player": "needs a speaker platform: i2s_audio is hardware, mixer and resampler wrap another speaker",
    "update": "the http_request platform is incompatible with the host framework; esp32_hosted needs hardware",
}

# What the integration must expose for the recipe's entity: sub-field -> (data type, role)
EXPECTED: dict[str, dict[str, tuple[T, R]]] = {
    "binary_sensor": {"state": (T.bool, R.sensor)},
    "sensor": {"state": (T.decimal, R.sensor)},
    "text_sensor": {"state": (T.string, R.sensor)},
    "switch": {"state": (T.bool, R.control)},
    "button": {"state": (T.none, R.control)},
    "number": {"state": (T.decimal, R.control)},
    "select": {"state": (T.enum, R.control)},
    "text": {"state": (T.string, R.control)},
    "lock": {"state": (T.enum, R.sensor), "command": (T.enum, R.control)},
    "valve": {
        "position": (T.decimal, R.control),
        "current_operation": (T.enum, R.sensor),
        "stop": (T.none, R.control),
    },
    "cover": {
        "position": (T.decimal, R.control),
        "operation": (T.enum, R.sensor),
        "tilt": (T.decimal, R.control),
        "stop": (T.none, R.control),
    },
    "fan": {
        "state": (T.bool, R.control),
        "speed_level": (T.integer, R.control),
        "oscillating": (T.bool, R.control),
        "direction": (T.enum, R.control),
        "preset_mode": (T.enum, R.control),
    },
    "light": {
        "state": (T.bool, R.control),
        "brightness": (T.decimal, R.control),
        "color_hue": (T.decimal, R.control),
        "color_saturation": (T.decimal, R.control),
        "color_temperature": (T.decimal, R.control),
        "effect": (T.enum, R.control),
        "color_brightness": (T.decimal, R.control),
        "transition_length": (T.decimal, R.control),
        "flash": (T.none, R.control),
    },
    "climate": {
        "mode": (T.enum, R.control),
        "action": (T.enum, R.sensor),
        "current_temperature": (T.decimal, R.sensor),
        "target_temperature_low": (T.decimal, R.control),
        "target_temperature_high": (T.decimal, R.control),
        "fan_mode": (T.enum, R.control),
        "swing_mode": (T.enum, R.control),
        "preset": (T.enum, R.control),
        "custom_preset": (T.enum, R.control),
        "current_humidity": (T.decimal, R.sensor),
        "target_humidity": (T.decimal, R.control),
    },
    "light/rgbw": {
        "state": (T.bool, R.control),
        "brightness": (T.decimal, R.control),
        "color_hue": (T.decimal, R.control),
        "color_saturation": (T.decimal, R.control),
        "white": (T.decimal, R.control),
        "color_brightness": (T.decimal, R.control),
        "transition_length": (T.decimal, R.control),
        "flash": (T.none, R.control),
    },
    "climate/single-point": {
        "mode": (T.enum, R.control),
        "action": (T.enum, R.sensor),
        "current_temperature": (T.decimal, R.sensor),
        "target_temperature": (T.decimal, R.control),
    },
    "date": {"date": (T.struct, R.control)},  # year, month, day as its fields
    "time": {"time": (T.struct, R.control)},  # hour, minute, second as its fields
    "datetime": {"epoch_seconds": (T.integer, R.control)},
    "alarm_control_panel": {
        "state": (T.enum, R.sensor),
        "command": (T.enum, R.control),
        "command_with_code": (T.none, R.control),  # command and code as its fields: this panel requires a code
    },
    "event": {},  # one-shot events have no parameters yet
    "water_heater": {
        "on": (T.bool, R.control),  # a flag of the state bit mask
        "away": (T.bool, R.control),
        "mode": (T.enum, R.control),
        "current_temperature": (T.decimal, R.sensor),
        "target_temperature": (T.decimal, R.control),
    },
}

# Fields of the entity's state or command that no parameter exposes, each with the reason. A field the library adds
# later is not listed: the sweep fails until it is mapped or explained here.
UNMAPPED: dict[str, dict[str, str]] = {
    "cover": {
        "current_operation": "exposed as operation",
        "legacy_state": "deprecated: position and operation replace it",
    },
    "fan": {"speed": "the legacy three-step speed: speed_level replaces it"},
    "light": {
        "red": "exposed as color_hue and color_saturation",
        "green": "exposed as color_hue and color_saturation",
        "blue": "exposed as color_hue and color_saturation",
        "rgb": "exposed as color_hue and color_saturation",
        "color_mode": "derived from the capabilities the light reports",
        "cold_white": "the channel behind color_temperature",
        "warm_white": "the channel behind color_temperature",
        "white": "only on RGBW lights: the light/rgbw recipe covers it",
        "flash_length": "exposed as the length of the flash command",
    },
    "climate": {
        "target_temperature": "two-point thermostat: low and high are exposed (climate/single-point covers the other)",
        "unused_legacy_away": "deprecated",
        "custom_fan_mode": "the host thermostat platform has none: tests/test_unhosted_fields.py covers it",
    },
    "light/rgbw": {
        **{f: "exposed as color_hue and color_saturation" for f in ("red", "green", "blue", "rgb")},
        "color_mode": "derived from the capabilities the light reports",
        "flash_length": "exposed as the length of the flash command",
        **{
            f: "the channels behind a colour temperature, which this light has not"
            for f in ("cold_white", "warm_white")
        },
        "color_temperature": "this light has no colour temperature",
        "effect": "this light has no effects",
    },
    "climate/single-point": {
        "target_temperature_low": "this thermostat is single-point: target_temperature is exposed instead",
        "target_temperature_high": "this thermostat is single-point: target_temperature is exposed instead",
        "unused_legacy_away": "deprecated",
        **{
            f: "this thermostat does not support it"
            for f in (
                "fan_mode",
                "swing_mode",
                "preset",
                "custom_preset",
                "custom_fan_mode",
                "current_humidity",
                "target_humidity",
            )
        },
    },
    "lock": {"code": "only offered when the lock requires a code: tests/test_unhosted_fields.py covers it"},
    "water_heater": {
        "state": "exposed as on and away, the flags of this bit mask",
        "target_temperature_low": "single-point: tests/test_unhosted_fields.py covers two-point",
        "target_temperature_high": "single-point: tests/test_unhosted_fields.py covers two-point",
    },
}


def kind_of(key: str) -> str:
    """The entity kind of a recipe key: `light` and `light/rgbw` are both lights."""
    return key.split("/")[0]


def all_kinds() -> list[str]:
    """Every entity kind the installed `aioesphomeapi` knows."""
    return list(COMPONENT_TYPE_TO_INFO)


def sketch_for(key: str) -> Sketch:
    index = list(RECIPES).index(key)
    name = "cat_" + key.replace("/", "_").replace("-", "_")
    return Sketch(f"{name}.yaml", name, API_PORT, f"983569{index:06x}", directory=NODES_DIR)


def recipe_keys() -> list[str]:
    """Every recipe: a kind's default one (`light`) and its variants (`light/rgbw`)."""
    return list(RECIPES)


def generate(kinds: list[str] | None = None) -> list[str]:
    """Write the node yaml of every kind that has a recipe; returns the kinds written."""
    NODES_DIR.mkdir(exist_ok=True)
    written = []
    for kind in kinds or recipe_keys():
        sketch = sketch_for(kind)
        body = RECIPES[kind]
        (NODES_DIR / sketch.yaml).write_text(
            HEADER.format(name=sketch.name, mac=_colon(sketch.mac), port=API_PORT) + body
        )
        written.append(kind)
    return written


def _colon(mac: str) -> str:
    return ":".join(mac[i : i + 2] for i in range(0, 12, 2))


def compile_node(sketch: Sketch) -> str | None:
    """Compile a node; returns None, or the compiler's last lines when it fails (the reason for the test failure)."""
    result = subprocess.run(
        ["esphome", "compile", str(sketch.directory / sketch.yaml)], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return None
    noise = ("blake2", "hashlib", "Traceback", "File ", "raise ", "~~", "^^", "globals(")
    lines = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if line.strip() and not any(n in line for n in noise)
    ]
    return f"esphome compile failed ({result.returncode}):\n" + "\n".join(lines[-15:])


def build_all(kinds: list[str] | None = None, workers: int = 3) -> dict[str, str | None]:
    """Generate and build the nodes in parallel; returns kind -> why it could not be built (None when built)."""
    if shutil.which("esphome") is None:
        raise RuntimeError("`esphome` CLI not found: install it (e.g. `pipx install esphome`)")
    todo = generate(kinds)

    def one(kind: str) -> tuple[str, str | None]:
        sketch = sketch_for(kind)
        return kind, None if sketch.binary.exists() else compile_node(sketch)

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
