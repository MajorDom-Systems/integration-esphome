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

Work lands on `develop`; `master` is protected and released via **Actions → Release**. Tests drive
the controller with the SDK's test doubles against a simulated ESPHome device — no physical
hardware required (see `tests/`).

## Run it standalone (without the Hub)

`majordom-esphome` is a standalone library — import it into your own app, or run **just this
integration** interactively (discover, pair, control, and inspect devices from a prompt) with no
Hub. It needs ESPHome devices reachable on the local network (Wi-Fi or Ethernet).

See **[Standalone mode](https://docs.majordom.io/device-integration/standalone)** for the
interactive CLI, watch mode, and the programmatic API.

## Supported transports (backend-agnostic)

The integration is transport-agnostic: it speaks to ESPHome devices over any supported transport,
so swapping from Wi-Fi to Ethernet needs **no source change**.

- **Native API (default):** encrypted TCP connection to the ESPHome native API port (`6053`).
- **REST / HTTP:** fallback polling via HTTP when the native API is disabled on the device.
- **Selecting the transport:** by default the integration attempts **native API first**, then
falls back to HTTP. Pin one explicitly with the `MAJORDOM_ESPHOME_TRANSPORT` env var
(`native` / `http`) or the `ESPHomeController.transport` attribute. See
`majordom_esphome/transport.py`.

## About this integration

- **Protocol / platform:** ESPHome via native API and HTTP REST.
- **Transport(s):** TCP/IP (Wi-Fi, Ethernet).
- **Supported devices:** ESPHome-flashed devices — lights, switches, sensors, climate, covers,
fans, number, select, text, button, lock, valve, etc.
- **Credentials needed to pair:** API encryption key (if enabled on the device); otherwise none.

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
| Application (7) | ESPHome Native API / HTTP REST | **this integration** |
| Transport (4) | TCP | OS / device firmware |
| Network (3) | IP (IPv4 / IPv6) | OS / device firmware |
| Data link / Physical (1–2) | Wi-Fi (802.11) or Ethernet | device hardware |

### Progress

Two checklists — this README is where you track them (tick items as you implement them and the
matching test in `tests/` goes green).

**Implementation** — makes the integration functional:

- [x] Discovery services registered via `self.dependencies.zeroconf_discovery_service`; cancel
closures saved and called in `stop`
- [x] Discovery service listeners fire when devices are found, and the controller calls
`self.dependencies.output.controller_did_receive_discovery`
- [x] Discovery of devices already paired to the Hub on reconnect, e.g. after a reboot
(`self.dependencies.output.controller_did_connect_device` is called)
- [x] `start_pairing_window` implemented (mDNS discovery window for new devices)
- [x] Device pairing
- [x] Device schema is properly mapped: device info, parameter list, and each parameter's metadata
are translated to MajorDom's domain model
- [x] Hub → Device control (`send_command` is implemented)
- [x] Device → Hub event subscription (`self.dependencies.output.controller_did_receive_events`
is called on incoming state changes)
- [x] `identify` is implemented (device-side LED / buzzer trigger via ESPHome service call)
- [x] `unpair` is implemented
- [x] `fetch` is implemented
- [x] Paired devices going offline/coming back online while the Hub is running — set
`device.available` accordingly (report `controller_did_lose_device`), and clear/set `last_error`
to match
- [x] Graceful shutdown in `stop`, cancelling any running tasks, discovery stopped, all
connections closed
- [x] Tests pass against a virtual/simulated ESPHome device (`tests/test_controller.py`)
- [x] README fully filled in

**Quality** — makes it reliable and maintainable (the bar for release):

- [x] **Recovers automatically** from connection loss / offline device / restarted backend —
retried with backoff, no manual restart
- [x] **No exception escapes the controller** — every background task, subscription loop, and
callback catches its own errors; nothing raised into `self.dependencies.output.*`
- [x] **Failures are surfaced, not raised** — logged once (no spam) and reflected on the device
(`available` / `last_error`), cleared on recovery
- [x] **Re-authenticates automatically** when the API key is rejected (if the device requires one)
- [x] **Fully asynchronous** — no blocking I/O on the event loop; heavy/blocking work runs off-loop
- [x] **Stable identity** — device and parameter UUIDs derived through the SDK helpers from the
device's MAC address, identical across restarts/re-pairs
- [x] **End-to-end tests** drive pair → command → fetch → events → `unpair` against a virtual
device (`majordom_integration_sdk.testing`)
- [x] **Failure paths tested** — offline device, transport error, rejected credentials degrade
gracefully (no raise)
- [x] **Broad device coverage** — lights, switches, sensors, climate, covers, fans, number,
select, text, button, lock, valve (virtual-device catalogue in CI)
- [x] **Fully typed** (`ty`, no package-wide ignores) and **clean** (`poe check`) with no warnings
- [x] **Readable & structured** — conversion logic in a mapper, models separated, comments where
intent isn't obvious
- [x] **Efficient** — subscriptions over polling; batch reads; no redundant work
- [x] **Diagnosable** — logging at the right levels to debug a device problem from logs alone
- [x] **Rich parameter metadata** — correct `visibility` per parameter and a sensible
`main_parameter`, so the app presents a clean control-center action and a tidy parameter list
([Parameter UX](https://docs.majordom.io/device-integration/parameter-ux))
- [x] **Owned** — a listed maintainer who keeps it working as the ESPHome/library evolve

### Parameter metadata sources & priority

Every parameter's UX metadata is resolved from several sources. Two independent axes, each with
its own priority ladder (first match wins). See also the
[parameter-ux recipe](https://docs.majordom.io/device-integration/parameter-ux).

**Visibility / role / unit** — resolved by `classify_entity()` in `esphome_spec.py`:

| # | Source | What it is |
|---|--------|-----------|
| 1 | `OUR_ENTITY_UX` (`VISIBILITY_OVERRIDES`, `USER_READINGS`, `EVERYDAY_CONTROL_ENTITIES`) | our hand curation — a human's call wins over everything |
| — | metadata / internal diagnostic entity | forced **system** (safety; debug counters & internal flags stay hidden) |
| 2 | **v2 quirk entity metadata** | per-device judgment from a loaded `esphomequirks` `QuirkBuilder` (`quirk_ux_map()`), runtime |
| 3 | `ESPHOME_ENTITY_UX` | standard-entity judgment **harvested** from `esphome` (`scripts/harvest_esphome.py`, vendored — `esphome` is not a runtime dep) |
| 4 | **fallback policy** | heuristic (sensor → user, switch/number → setting); **logs a warning** so uncurated entities surface. Flip `_FALLBACK_HIDE_UNCURATED` to hide-by-default once coverage is validated on real devices. |

**Bounds (`min`/`max`/`step`)** — a separate ladder (`resolve_metadata_bounds()`):

1. the device's own limit attributes' **runtime values** (`METADATA_SOURCES`) — ground truth for
*this* device;
2. ESPHome component schema tables (`ENTITY_MIN_STEP`, platform range);
3. platform default. A missing expected limit is logged (quirk detection).

**Quirks.** `esphomequirks.setup()` runs once at controller startup so discovered devices are
presented in quirked form (manufacturer entities decoded into named/typed attributes; v2 entity
metadata attached). This requires the ESPHome 2024.x+ stack.

**Drift.** `scripts/check_esphome_drift.py` re-harvests `esphome` and diffs against the vendored
artifact via the SDK's `diff_specs`, tiering changes ADD / REMOVE / **RECLASSIFY** (high-risk —
changes what current users already see). CI opens a Dependabot-style refresh PR on drift.

### Notes

The device/parameter ids are derived from the device's MAC address via the SDK's UUID helpers, so
they're stable across restarts and namespaced per integration.

ESPHome devices must have the `api:` component enabled in their YAML for the native API transport
to work. If the device only exposes a web server, the integration falls back to HTTP REST polling.

## License

See [LICENSE](LICENSE). For commercial licensing or partnership inquiries regarding MajorDom,
contact us via [parker-industries.org/partnership](https://parker-industries.org/partnership).
