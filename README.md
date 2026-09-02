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

<!-- ┌───────────────────────────────────────────────────────────────────────────┐
     │ USING THIS TEMPLATE — delete everything down to the "DELETE ABOVE" line      │
     │ once your integration repo is set up. What's left below is your own README.  │
     └───────────────────────────────────────────────────────────────────────────┘ -->

# Creating a new MajorDom integration from this template

Click **Use this template → Create a new repository** (name it `integration-<protocol>`,
e.g. `integration-hue`), then:

**1. Rename the placeholders** (find & replace across the repo)
- `integration-template` → your distribution name, by convention `majordom-<protocol>`
  (hyphens): in `pyproject.toml` (`[project].name`) and `.github/workflows/release.yml`
  (`pypi-package-name`).
- `integration_template` → your import name `majordom_<protocol>` (underscores): the
  `integration_template/` directory, plus the `--cov=` and `packages` references in
  `pyproject.toml`.
- Rename `ExampleController` and fill in `integration_template/controller.py` with real
  protocol logic. `tests/test_controller.py` is prefilled to fail until you do — work the
  **Progress** checklist below and tick items off as your CI goes green.
- In `.github/workflows/test.yml`, delete the `template-selfcheck` job and the `if:` guard
  on the `test` job — they exist only to keep the *template* repo green and aren't wanted in
  a real integration.
- Fill in the **About this integration** section below (it doubles as your PR summary — a
  reviewer reads the checklist to see what's actually implemented).

**2. Install + pre-commit hook**
```sh
pip install poetry poethepoet && poe install
```

**3. Create the `develop` branch**
```sh
git checkout -b develop && git push origin develop
```

**4. GitHub repo settings** — same as any package repo built on the shared workflows:

- **General** → enable **Allow auto-merge**;  
- **Branches** → protect `master` (require the `test / check` status, restrict push to `github-actions[bot]`) and `develop` (require `test / check`);  
- **Environments** → create `release` (deployment branch `develop` only, required reviewer). Then configure PyPI trusted publishing for your package (Owner = the GitHub user or org that owns **this** repo, Repository = this repo, Workflow `release.yml`, Environment `release`).  

The reusable CI/CD lives in [ParkerIndustries/workflows](https://github.com/ParkerIndustries/workflows).

<!-- ─────────────────────────────── DELETE ABOVE ─────────────────────────────── -->

# integration-template

A [MajorDom](https://majordom.io) integration — bridges the Example protocol into the
MajorDom language.

Built for the **MajorDom Hub**, but it doesn't need it: this is a standalone, standardized
library for the Example protocol that you can use on its own (see **Run it standalone** below).
Built on the [MajorDom Integration SDK](https://github.com/MajorDom-Systems/integration-sdk).
The integration's entry point is a `Controller` (`integration_template/controller.py`) that the
Hub — or the SDK's dev runner — instantiates and drives through its lifecycle: discovery →
pairing → commands → teardown.

- **Other protocols:** browse the [MajorDom integrations](https://github.com/orgs/MajorDom-Systems/repositories?q=integration-).
- **Create your own:** start from the [integration template](https://github.com/MajorDom-Systems/integration-template).

## Documentation

Full integration-author docs — the controller lifecycle, data models, storing data,
discovery, and a worked example — live at **[docs.majordom.io](https://docs.majordom.io/device-integration)**.

## Development

```sh
poetry install && poetry run poe install
```

| Task | Description |
|------|-------------|
| `poe check` | Full quality pipeline (ruff, ty, pytest, poetry build/check) |
| `poe check --ci` | Same, plus `git diff --exit-code` |

Work lands on `develop`; `master` is protected and released via **Actions → Release**.
Tests drive the controller with the SDK's test doubles (`majordom_integration_sdk.testing`)
against a virtual/simulated device — see `tests/`.

## Run it standalone (without the Hub)

Your integration is a standalone library — import it into another app, or run **just this
integration** interactively (discover, pair, control, and inspect devices from a prompt) with no Hub.
Note here whatever prerequisites your protocol needs to run on its own (a broker, a radio, a local
server, …).

See **[Standalone mode](https://docs.majordom.io/device-integration/standalone)** for the interactive
CLI, watch mode, and the programmatic API.

## About this integration

<!-- Fill this in. It's your PR summary — keep the checklist current so a reviewer sees at
     a glance what works. -->

- **Protocol / platform:** _e.g. Philips Hue (Zigbee via a bridge)_
- **Transport(s):** _wifi / ble / zigbee / …_
- **Supported devices:** _…_
- **Credentials needed to pair:** _none / code / secret / qr_

### Required harness

<!-- EXAMPLE — replace with your integration's real requirements (delete rows that don't
     apply). External things this integration needs to run, beyond `pip install`, so a
     deployment knows what to provide. -->

- **Hardware adapters:** _e.g. an 802.15.4 radio (SkyConnect / a Thread or Zigbee dongle), a
  USB serial gateway …_ — the Hub assigns OS device paths through
  `dependencies.hardware_interfaces` (e.g. `/dev/ttyACM0`).
- **Third-party software services:** _e.g. an OpenThread Border Router (OTBR), a vendor
  bridge/hub, an MQTT broker, a matter-server instance …_ — what must be running and reachable.
- **OS / permissions:** _e.g. Bluetooth access, host networking, mDNS/SSDP on the LAN …_

### Protocol stack

Every integration is two things stacked: the **MajorDom integration layer** — mapping the
protocol to MajorDom's domain model — sitting on top of the **protocol stack** it bridges. The
top layer is *always this repo*. How much of the stack *below* it is also this repo's code
varies: some integrations only map an existing application-level protocol (a vendor library / the
OS / the harness provides everything under them), while others implement the protocol themselves,
down to raw UDP or even a custom radio.

<!-- EXAMPLE — the table below is a Matter-over-Thread illustration; replace every row with your
     own stack. Mark which layers are THIS repo's code vs. a library, the OS, or the harness —
     it sets the scope of what the integration owns. Keep only the rows that apply. -->

| Layer | Protocol | Implemented by |
|-------|----------|----------------|
| **MajorDom integration** | maps the protocol ↔ MajorDom domain model | **this repo, always** |
| Application (7) | _Matter clusters / data model_ | this integration (via `chip` lib) |
| Session (5) | _CASE / PASE secure session_ | library |
| Transport (4) | _UDP_ | OS |
| Network (3) | _IPv6 · 6LoWPAN_ | OS · OTBR (harness) |
| Data link / Physical (1–2) | _Thread · IEEE 802.15.4_ | radio adapter (harness) |

If your integration implements the protocol itself (no vendor library), more of the lower rows
become **this integration** — a custom-radio integration can own everything from the application
layer down to the physical.

### Progress

Two checklists — this README is where you track them (tick items as you implement them and the
matching test in `tests/` goes green). The docs explain the *why* behind each item:
[Implementation Checklist](https://docs.majordom.io/device-integration) (gets it working) and
[Quality Checklist](https://docs.majordom.io/device-integration/quality) (gets it releasable).

**Implementation** — makes the integration functional:

- [ ] Discovery services registered via `self.dependencies.zeroconf_discovery_service`, `ssdp_discovery_service`, and/or `ble_discovery_service` as appropriate; cancel closures saved and called in `stop`
- [ ] Discovery service listeners fire when devices are found, and the controller calls `self.dependencies.output.controller_did_receive_discovery`
- [ ] Discovery of devices already paired to the Hub on reconnect, e.g. after a reboot (`self.dependencies.output.controller_did_connect_device` is called)
- [ ] `start_pairing_window` is implemented but only if the protocol requires an explicit scan (like zigbee)
- [ ] Device pairing 
- [ ] Device schema is properly mapped: device info, parameter list, and each parameter's metadata are translated to MajorDom's domain model
- [ ] Hub → Device control (`send_command` is implemented)
- [ ] Device → Hub event subscription (`self.dependencies.output.controller_did_receive_events` is called on incoming events)
- [ ] `identify` is implemented
- [ ] `unpair` is implemented
- [ ] `fetch` is implemented
- [ ] Paired devices going offline/coming back online *while the Hub is running* (not just on reboot) — set `device.available` accordingly (report `controller_did_lose_device`), and clear/set `last_error` to match
- [ ] Graceful shutdown in `stop`, cancelling any running tasks, discovery stopped, all connections closed
- [ ] Tests pass against a virtual/simulated device (`tests/test_controller.py`)
- [ ] README fully filled in: delete the template-setup section above, complete **About this integration**, **Required harness**, **Protocol stack**, and **Notes** with real content

**Quality** — makes it reliable and maintainable (the bar for release):

- [ ] **Recovers automatically** from connection loss / offline device / restarted backend — retried with backoff, no manual restart
- [ ] **No exception escapes the controller** — every background task, subscription loop, and callback catches its own errors; nothing raised into `self.dependencies.output.*`
- [ ] **Failures are surfaced, not raised** — logged once (no spam) and reflected on the device (`available` / `last_error`), cleared on recovery
- [ ] **Re-authenticates automatically** when credentials expire/are rejected (if the protocol uses credentials)
- [ ] **Fully asynchronous** — no blocking I/O on the event loop; heavy/blocking work runs off-loop
- [ ] **Stable identity** — device and parameter UUIDs derived through the SDK helpers, identical across restarts/re-pairs
- [ ] **End-to-end tests** drive pair → command → fetch → events → `unpair` against a virtual device (`majordom_integration_sdk.testing`)
- [ ] **Failure paths tested** — offline device, transport error, rejected credentials degrade gracefully (no raise)
- [ ] **Broad device coverage** where the protocol has many device/parameter types (a virtual-device catalogue in CI is ideal)
- [ ] **Fully typed** (`ty`, no package-wide ignores) and **clean** (`poe check`) with no warnings
- [ ] **Readable & structured** — conversion logic in a mapper, models separated, comments where intent isn't obvious
- [ ] **Efficient** — subscriptions over polling; batch/chunk reads; no redundant work
- [ ] **Diagnosable** — logging at the right levels to debug a device problem from logs alone
- [ ] **Rich parameter metadata** — correct `visibility` per parameter and a sensible `main_parameter`, so the app presents a clean control-center action and a tidy parameter list ([Parameter UX](https://docs.majordom.io/device-integration/parameter-ux))
- [ ] **Owned** — a listed maintainer who keeps it working as the protocol/library evolve
- [ ] _nice-to-have:_ localizable naming · firmware/software updates (where supported) · user-facing supported-device docs

### Notes

<!-- EXAMPLE / optional — free text. Anything a reader should know that the checklist can't
     capture. Delete this comment and write your own, or remove the section if unused. -->

_e.g. "Hobby project, maintained best-effort." · "IP transport works; BLE pairing is not
implemented yet." · "Help wanted: reliable re-pair after a bridge reboot." · known quirks,
firmware versions tested against, limitations._

## License

See [LICENSE](LICENSE). Your integration code is yours to license as you choose. For
commercial licensing or partnership inquiries regarding MajorDom, contact us via
[parker-industries.org/partnership](https://parker-industries.org/partnership).
