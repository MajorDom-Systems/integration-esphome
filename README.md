<!-- MajorDom Project Banner -->
<a href="https://majordom.io" target="_blank">
  <picture>
    <source
      media="(prefers-color-scheme: dark)"
      srcset="https://markparker.me/banners/majordom-dark.webp"
    />
    <img
      alt="Part of MajorDom — the next-gen smart home"
      src="https://markparker.me/banners/majordom-light.webp"
    />
  </picture>
</a>

# integration-esphome

A [MajorDom](https://majordom.io) integration — bridges **ESPHome** devices into the
MajorDom language.

Built for the **MajorDom Hub**, but it doesn't need it: this is a standalone, standardized
library for ESPHome that you can use on its own (see **Run it standalone** below). Built on the
[MajorDom Integration SDK](https://github.com/MajorDom-Systems/integration-sdk). The entry point
is `ESPHomeController` (`majordom_esphome/controller.py`), which the Hub — or the SDK's dev
runner — instantiates and drives through its lifecycle: discovery → pairing → commands →
teardown.

- **Other protocols:** browse the [MajorDom integrations](https://github.com/orgs/MajorDom-Systems/repositories?q=integration-).
- **Create your own:** start from the [integration template](https://github.com/MajorDom-Systems/integration-template).

## Documentation

Full integration-author docs — the controller lifecycle, data models, storing data, discovery,
and a worked example — live at **[docs.majordom.io](https://docs.majordom.io/device-integration)**.

## Development

```sh
poetry install && poetry run poe install
```

| Task | Description |
|------|-------------|
| `poe check` | Full quality pipeline (ruff, ty, pytest, poetry build/check) |
| `poe check --ci` | Same, plus `git diff --exit-code` |

Work lands on `develop`; `master` is protected and released via **Actions → Release**.

Tests drive the controller with the SDK's test doubles against a **virtual ESPHome device**: a
native binary built from `tests/virtual/sketches/*.yaml` (ESPHome `host` platform, one entity of
every supported kind, with and without API encryption) — no physical hardware required. The
binaries are built on first use and need the ESPHome CLI:

```sh
pipx install esphome && poetry run python -m tests.virtual.build
```

## Run it standalone (without the Hub)

`majordom-esphome` is a standalone library — import it into your own app, or run **just this
integration** interactively (discover, pair, control, and inspect devices from a prompt) with no
Hub. It needs ESPHome devices reachable on the local network (Wi-Fi or Ethernet).

See **[Standalone mode](https://docs.majordom.io/device-integration/standalone)** for the
interactive CLI, watch mode, and the programmatic API.

## Supported transports

The integration speaks the **ESPHome native API** over TCP (port `6053` by default), with or without
Noise encryption. Devices must have the `api:` component enabled in their YAML; an API-less device
(e.g. web server only) is not supported.

## About this integration

- **Protocol / platform:** ESPHome native API (`aioesphomeapi`).
- **Transport(s):** TCP/IP (Wi-Fi, Ethernet); devices are found via mDNS (`_esphomelib._tcp`).
- **Supported entities:** light (on/off, brightness, RGB), switch, sensor, binary sensor, text
sensor, number, select, button, cover (position), fan (on/off), climate (mode, targets).
Every other entity the library knows (lock, valve, text, alarm panel, update, ...) is mapped generically from
the library's own types (see below); cameras and events are skipped.
- **Credentials needed to pair:** the API encryption key (type `secret`) if the node has one
configured; otherwise none.

### Required harness

- **Hardware adapters:** none — ESPHome devices are self-contained and connect over the network.
- **Third-party software services:** none — the integration speaks directly to ESPHome devices.
- **OS / permissions:** network access to the ESPHome devices (same LAN or routable subnet);
mDNS/SSDP may be required for discovery depending on configuration.

### Protocol stack (OSI)

Every integration is two things stacked: the **MajorDom integration layer** — mapping the
protocol to MajorDom's domain model — sitting on top of the **protocol stack** it bridges. The
top layer is *always this repo*.

| Layer | Protocol | Implemented by |
|-------|----------|----------------|
| **MajorDom integration** | maps ESPHome ↔ MajorDom domain model | **this repo, always** |
| Application (7) | ESPHome Native API | [`aioesphomeapi`](https://github.com/esphome/aioesphomeapi) |
| Transport (4) | TCP | OS / device firmware |
| Network (3) | IP (IPv4 / IPv6) | OS / device firmware |
| Data link / Physical (1–2) | Wi-Fi (802.11) or Ethernet | device hardware |

### Progress

Two checklists. An item is ticked only when a test in `tests/` proves it.

**Implementation** — makes the integration functional:

- [x] Discovery service registered via `self.dependencies.zeroconf_discovery_service`; the cancel
closure is called in `stop`
- [x] Discovery listener methods fire when nodes appear, change and leave, and the controller calls
the matching `self.dependencies.output.controller_did_*_discovery`
- [x] Paired devices reconnect when the Hub restarts (`controller_did_connect_device` is called)
- [x] `start_pairing_window`: not needed, mDNS discovery is always on (SDK default is a no-op)
- [x] Device pairing, including the encryption key (`CredentialsType.secret`)
- [x] Device schema is mapped from the entities the device reports: parameters, units, limits and
valid values
- [x] Hub → Device control (`send_command`)
- [x] Device → Hub event subscription (`controller_did_receive_events` on every state change)
- [x] `identify`: ESPHome has no identify action, so it is a documented no-op
- [x] `unpair`
- [x] `fetch` reports the current value of every parameter in one batch
- [x] Paired devices going offline / coming back online while the Hub is running update
`device.available` and `last_error`, and call `controller_did_lose_device` /
`controller_did_connect_device`
- [x] Graceful shutdown in `stop`: discovery stopped, connections closed, no tasks left running
- [x] Tests pass against a virtual ESPHome device (`tests/`)
- [x] README matches the code

**Quality** — makes it reliable and maintainable (the bar for release):

- [x] **Recovers automatically** from connection loss / offline device / restarted device: retried
with exponential backoff (1 s up to 30 s), no manual restart
- [x] **No exception escapes** discovery handlers, state callbacks or background tasks
- [x] **Failures are surfaced** on the device (`available` / `last_error`) in plain language and
cleared on recovery; a rejected encryption key is reported as such
- [x] **Fully asynchronous**: no blocking I/O on the event loop
- [x] **Stable identity**: device and parameter UUIDs derive through the SDK helpers
(`device_uuid`, `parameter_uuid`) from the node's MAC address and the entity's `object_id`
- [x] **End-to-end tests** drive discovery (over a stubbed zeroconf, never the real network) → pair → state → command →
fetch → restart → outage → `unpair` against a virtual device, with and without encryption
- [x] **Failure paths tested**: unreachable device, wrong or missing key, unsupported credentials,
command on a read-only parameter, out-of-range values, offline device
- [x] **Broad device coverage**: the entities above by hand, every other entity generically (tested with a lock, valve, text, date and alarm panel)
- [x] **Readable & structured**: conversion logic in a mapper, models separated
- [x] **Efficient**: subscriptions instead of polling; states are delivered in order
- [x] **Diagnosable**: connection failures and unexpected errors are logged with their cause
- [x] **Parameter metadata**: `visibility` follows the entity category, and `main_parameter` is a
one-tap toggle
- [ ] **Owned**: a listed maintainer who keeps it working as ESPHome and the library evolve

### Parameter mapping

Parameters are built from what the device reports (`list_entities_services`), never from fixed
defaults:

| Entity | Parameters |
|--------|------------|
| switch, fan | `state` (bool) |
| light | `state`, `brightness` (%) if the light dims, `color_hue` (°) and `color_saturation` (%) if it supports RGB |
| cover | `position` (%), `operation` (`IDLE` / `IS_OPENING` / `IS_CLOSING`, read-only) |
| climate | `mode` (`enum` of the modes it supports), `current_temperature`, `target_temperature` or `target_temperature_low/high` |
| number | `state` with the device's `min` / `max` / `step` |
| select | `state` as the option index (`enum`, `valid_values` labels are the options) |
| sensor, binary sensor, text sensor | `state` (read-only) |
| button | `state` (fires the press) |

Brightness, positions and saturation are percentages (0–100, ESPHome reports 0–1); colour is hue in degrees
plus saturation, the model the Matter and Zigbee integrations use, converted from ESPHome's RGB. Enums are
integers with string labels in `valid_values`, labelled by the ESPHome enum names. Units come from the
entity's own unit string, else from its device class. Entities in the
`config` / `diagnostic` category are `setting`, and entities disabled by default are `system`.

### Remaining fields

The hand-written mapping above gives the everyday parameters. Every other state or command field an entity supports is
added by the generic mapping as a `setting` parameter (`majordom_esphome/generic.py`), so nothing the library reports is
dropped silently: a fan's speed level, direction, oscillation and presets; a cover's tilt and stop; a light's colour
temperature, white and effects; a climate's action, fan, swing and preset modes and humidity. A field is only added when
the entity says it supports it, enums are limited to the supported members, option lists (effects, presets) become
enums like a select, and the limits the entity reports (speed count, mireds, humidity) are used. The few fields that
are no values (a command's `code` argument, a light's `flash_length`) are listed with the reason in `UNMAPPED` in the
catalogue, which fails when a new field appears that is neither mapped nor explained.

### Generic mapping

Entities without a hand-written mapping get parameters from `aioesphomeapi`'s own types (`majordom_esphome/generic.py`):
one parameter per field of the entity's state (read-only, unless the entity's command accepts it) and one per
command argument that can be sent on its own. `bool`, `int`, `float` and `str` map directly, the library's integer
enums become `enum` parameters labelled with their member names, fractions such as a position or volume become
percentages, and a command-only flag (a valve's `stop`) becomes a button. Commands with several required arguments
(a date) are read-only. A hand-written mapping wins wherever the generic result is not good enough for the user.

### Entity catalogue

`tests/test_catalogue.py` and `tests/virtual/catalogue.py` mirror the Matter integration's virtual-device catalogue.
The list of entity kinds is whatever the installed `aioesphomeapi` knows. Fast checks (always run) fail when a kind is
neither mapped by the integration nor skipped on purpose, or has neither a recipe nor a reason it cannot run on the
host node. Recipes are written by hand, never guessed: each one switches on every capability of its entity and comes
with the exact parameters it must produce (`EXPECTED`) and every state or command field that is deliberately not
exposed, with the reason (`UNMAPPED`), so a field ESPHome adds later fails the sweep until it is mapped or explained. The sweep (`poetry run pytest -m catalogue`) builds one virtual node per recipe in parallel, pairs it, checks
its parameters against `EXPECTED` and `UNMAPPED`, reads its states, sends a valid command to every control parameter and checks that the connection survives and every value
has the declared type. `.github/workflows/canary.yml` runs everything monthly against the latest ESPHome and
`aioesphomeapi` and goes red when upstream adds something unsupported.

### Notes

The device id derives from the node's MAC address (mDNS `mac` TXT record) via the SDK's UUID
helpers, so ids are stable across restarts and namespaced per integration.

ESPHome devices must have the `api:` component enabled in their YAML.

Connecting: a node is reached through its hostname first, then through the IP addresses it announced.
The first address that works moves to the front and the ones that failed move to the end, so the
next connection starts with what worked last time. When a paired node announces itself again (e.g.
after a DHCP change) its address list is refreshed.

## License

See [LICENSE](LICENSE). For commercial licensing or partnership inquiries regarding MajorDom,
contact us via [parker-industries.org/partnership](https://parker-industries.org/partnership).
