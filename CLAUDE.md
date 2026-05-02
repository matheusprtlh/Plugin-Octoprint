# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Single-deliverable repo: an OctoPrint plugin that posts printer state to a webhook URL configured by the user. There is **no** marketplace, backend, or sibling daemon — earlier iterations of this work explored a centralized server design, but the contract scope is just the plugin.

The repo holds:

- `plugin/` — `OctoPrint-MarketplaceLink` source (the deliverable). Includes `README.md` written for the end-user / client.
- `plugin/dist/` — built artifacts (wheel + sdist) ready to ship to the client.
- `test_e2e/` — harness that exercises the plugin against a real `requests`-based webhook receiver, with stubs of `octoprint.plugin` / `octoprint.util`. Validated 9/9 deliveries with correct payloads, schema_version, bearer header, and error telemetry.
- `test_env/` — venv with `requests` + `build` for running the harness and producing wheels.
- `Contexto de Projeto - Transcrições e Requisitos.md` — full WhatsApp audio transcripts (pt-BR) plus an executive summary of the original ask.
- Three `.ogg` WhatsApp audio files — originals of the transcripts.

## Scope (from the transcripts)

Verbatim from the customer's audios:

- An OctoPrint plugin that lets a user **integrate their printer with the customer's website**. The customer's website is outside this repo's scope — this plugin only POSTs JSON; whoever runs the receiving site decides the data model.
- **Multi-brand mandatory**: Bambu Lab, Creality, Elegoo, Prusa, "all printers". The plugin is brand-agnostic at the protocol layer (it just exposes a `brand` field).
- **Bambu Lab is now first-class** (since v0.4.0). Customer reported most of his customers run Bambu, so the original "out of scope" framing was retired. The plugin ships a built-in MQTT bridge (`octoprint_marketplace_link/bambu.py`) that connects directly to the Bambu's local TLS broker on port 8883, derives `PrintStarted`/`PrintPaused`/etc. from the `gcode_state` stream, and funnels them through the same `_post`/`_record_delivery` path as the serial-mode events. Same `schema_version: 1` payload, same webhook, same telemetry — the receiving site can't tell the two modes apart. **Caveat that remains**: Bambu firmware ≥ `01.07.x` (A1/P1S) and ≥ `01.08.x` (X1C) restricts LAN MQTT — users on those firmwares need Developer Mode enabled or a firmware rollback. README documents this.

Budget anchor (for scope discussions only): R$ 500 was quoted for the bundled tasks (this plugin + an unrelated Anvisa/Light medical-prescription automation tracked elsewhere). Push back on scope creep that obviously exceeds this.

## Plugin (`plugin/`)

`OctoPrint-MarketplaceLink` v0.4.0, identifier `marketplace_link`, package `octoprint_marketplace_link`. Pure Python, depends on `requests` (ships with OctoPrint) and `paho-mqtt>=1.6` (added in 0.4.0 for the Bambu bridge — pulled in automatically by pip).

Mixins used: `SettingsPlugin`, `AssetPlugin`, `TemplatePlugin`, `StartupPlugin`, `ShutdownPlugin`, `SimpleApiPlugin`, `EventHandlerPlugin`. Outbound only — the plugin never serves traffic on a public port; the Bambu bridge is a TLS *client* connecting outward to the printer on the LAN.

### What it does

Two state sources funnel into the same webhook path:

1. **Serial mode (default)**: on any of these OctoPrint events — `Connected`, `Disconnected`, `PrintStarted`, `PrintDone`, `PrintFailed`, `PrintCancelled`, `PrintPaused`, `PrintResumed`, `Error` — the plugin POSTs JSON to the configured webhook URL.
2. **Bambu mode (opt-in via `bambu_enabled`)**: instead of (or alongside — Bambu mode bypasses serial probes) listening to OctoPrint's bus, a background thread connects to `mqtts://<bambu_host>:8883`, subscribes to `device/<bambu_serial>/report`, and translates `gcode_state` transitions into the same event names. The webhook payload is identical except `printer.brand == "bambu"`, `printer.firmware_version` comes from `info.module`, and `job.file`/`progress` come from Bambu's `subtask_name`/`mc_percent`.

In both modes, a periodic `heartbeat` event fires every N minutes (default 5; 0 disables) so the receiving site can detect dead printers without waiting for a print event.

### Configuration

Settings page (Settings → Marketplace Link) takes:

- `webhook_url` — full URL receiving the POSTs.
- `auth_token` — optional Bearer token, sent in `Authorization: Bearer <token>`. Restricted to admin scope via `get_settings_restricted_paths()` so non-admins can't read it through OctoPrint's settings API.
- `brand` — Creality / Elegoo / Prusa / Bambu Lab / other.
- `model` — free text (e.g. "Ender 3 V2").
- `heartbeat_minutes` — int, 0–60. 0 disables.
- `bambu_enabled` — bool. Toggle the Bambu MQTT bridge.
- `bambu_host` / `bambu_serial` / `bambu_access_code` — Bambu LAN connection params. `bambu_access_code` is in the `admin`-restricted scope alongside `auth_token`.
- `bambu_model` — optional dropdown (A1 / A1 Mini / P1S / P1P / X1C); when set, overrides the generic `model` field in payloads built in Bambu mode.

A "Testar webhook" button POSTs a synthetic `event: "test"` payload immediately and reports the HTTP status; this path skips persisting the result so a manual test failure doesn't pollute the live delivery telemetry.

A separate "Testar conexão Bambu" button (only visible when `bambu_enabled`) builds a transient MQTT client with the current settings, attempts a CONNACK with an 8s timeout, and surfaces a localized error message — `_conn_rc_message()` in `bambu.py` translates paho rc codes (4 = bad access code, 5 = LAN mode locked) into actionable Portuguese.

### Payload shape

```json
{
  "schema_version": 1,
  "timestamp": "2026-04-29T13:45:01+00:00",
  "event": "PrintStarted",
  "status": "printing",
  "printer": {
    "name": "OctoPrint",
    "brand": "creality",
    "model": "Ender 3 V2",
    "firmware_version": "Marlin 2.0.x"
  },
  "job": {
    "file": "benchy.gcode",
    "progress": 0.0
  }
}
```

`schema_version` bumps only on non-additive changes (renames, removals, type changes). Adding a new optional field does **not** bump.

`job` is `null` unless `status == "printing"`. `firmware_version` is `null` if OctoPrint hasn't received the firmware banner yet. `event` is the OctoPrint event name verbatim, plus the synthetic values `"test"` (manual button) and `"heartbeat"` (periodic).

### Telemetry persisted in settings

After every (non-test) POST attempt the plugin records `last_delivery_at` (ISO UTC), `last_delivery_status` (HTTP code or 0 for network failure), and `last_delivery_error` (truncated body or exception string). The settings UI and the navbar widget poll `GET /api/plugin/marketplace_link` every 5 s and reflect this.

### Navbar widget

`templates/marketplace_link_navbar.jinja2` renders a Font Awesome icon + tooltip in OctoPrint's top bar:

- Gray broken-link → not configured.
- Green cloud → last delivery succeeded (or no delivery attempted yet, but URL is configured).
- Yellow warning triangle → last delivery failed (HTTP 4xx/5xx or network error).

Click navigates to the settings page anchor.

### Threading

Webhook POSTs run in a background daemon thread (`threading.Thread`) so they never block OctoPrint's event loop. `self._send_lock` serializes deliveries — a stuck POST won't overlap with the next event's POST. Heartbeat uses `octoprint.util.RepeatedTimer`.

The Bambu bridge owns its own daemon thread (`MarketplaceLinkBambu`). It runs `paho.mqtt.Client.loop_forever()` inside a wrapper that handles exponential reconnect (1s → 2s → ... → 60s cap). The bridge calls back into the plugin via two callbacks injected at construction: `event_callback(event_name)` for derived state events and `status_callback(snapshot)` for the latest job/printer state. **The order matters**: in `bambu._handle_report()`, `status_callback` fires *before* `event_callback`, so when the plugin builds a webhook payload inside the event handler, the snapshot it reads is already up to date. Getting that order wrong means `PrintStarted` would arrive with `status="idle"` and `job=null`.

### Plugin dev install

OctoPrint plugins must run inside the same Python venv as OctoPrint itself. OctoPrint supports Python 3.9–3.12 (do not use 3.13).

```bash
python -m venv ~/octoprint-dev && source ~/octoprint-dev/bin/activate
pip install OctoPrint
pip install -e plugin/                 # editable install of our plugin
octoprint serve                        # opens http://localhost:5000
```

In the OctoPrint UI: Settings → Marketplace Link → fill webhook URL + token → "Testar webhook". Watch `octoprint.log` for `Marketplace Link starting` and any delivery warnings.

### Build wheel + sdist

`test_env/` is a venv pre-provisioned with `build` and `requests` for this purpose. From `plugin/`:

```bash
python -m build                        # produces plugin/dist/*.whl + *.tar.gz
```

`test_env_39/` is the same setup pinned to Python 3.9 to verify the lower bound of `python_requires`.

### E2E harness

There are no plugin unit tests — mocking `octoprint.plugin` is more work than it's worth. Instead, `test_e2e/` runs the plugin against stubs of `octoprint.plugin` / `octoprint.util` and a real `requests`-based webhook receiver. Two processes, two terminals:

```bash
# Terminal 1 — receiver writes JSONL to test_e2e/received.log
python test_e2e/webhook_receiver.py --reset-log

# Terminal 2 — exerciser. Imports plugin source from plugin/ by default.
python test_e2e/run_test.py
```

Set `PLUGIN_USE_INSTALLED=1` before `run_test.py` to import the plugin from site-packages instead of `plugin/` — useful for smoke-testing the wheel produced by `python -m build`.

The harness fires the typical event sequence (Connected → PrintStarted → Paused → Resumed → Done → Disconnected), then the test-webhook button, then a 1s heartbeat, then a connection-refused failure to exercise the error-recording path. Last validated 9/9 deliveries with correct payloads, schema_version, bearer header, and error telemetry.

`test_e2e/run_bambu_test.py` covers the Bambu mode. Standing up a real TLS Bambu broker is overkill for what we need to verify — the actual logic is `BambuBridge._handle_report`, a pure state machine. The harness instantiates `BambuBridge` directly (skipping `start()` and the MQTT thread), wires it to the plugin, then injects a hand-crafted Bambu-shaped report sequence: `info.module` (firmware probe) → IDLE → PREPARE → RUNNING → PAUSE → RUNNING → FINISH → IDLE → PREPARE → RUNNING → FAILED, plus a final `bridge.stop()` to exercise the Disconnected path. It validates: event sequence, `printer.brand == "bambu"`, `printer.model == "P1S"` (from `bambu_model` overriding the dropdown), `firmware_version == "01.06.00.00"` (from `info.module.ota.sw_ver`), `status == "printing"` and `job.file == "benchy.3mf"` on the `PrintStarted` payload, `schema_version == 1` (no regression), and the `Authorization: Bearer` header still flowing. Run with `python test_e2e/run_bambu_test.py` while `webhook_receiver.py` is running. Last validated 8/8 checks pass.

### Deployment / distribution

Three install paths for end users, in increasing order of polish:

1. **GitHub-zip URL (fastest path to v0).** Tag a release on the plugin repo, then users go to `Settings → Plugin Manager → Get More → ...from URL` and paste `https://github.com/<org>/OctoPrint-MarketplaceLink/archive/v0.3.0.zip`. OctoPrint runs `pip install <url>` inside its venv and prompts a restart.
2. **PyPI.** `python -m build && twine upload dist/*` from `plugin/`. Users paste the package name `OctoPrint-MarketplaceLink` into Plugin Manager.
3. **Official Plugin Repository (long-term).** Submit a PR to https://github.com/OctoPrint/plugins.octoprint.org adding a YAML metadata file. Once merged, the plugin shows up in `Plugin Manager → Browse` with no URL needed. Requires a public GitHub repo, a working README, screenshots, and a pinned release.

### Plugin auto-update

`get_update_information()` declares a `github_release` source for OctoPrint's `softwareupdate` plugin, currently pointing at `matheusprtlh/Plugin-Octoprint` (constants `GITHUB_USER` / `GITHUB_REPO` in `__init__.py`). The function still has a sentinel-based kill switch: if `GITHUB_USER` is reset to `"TODO-org"` it returns `{}` and OctoPrint skips polling. Auto-update only delivers real releases once a public repo with matching tag exists — the README install URL (`.../archive/v0.3.0.zip`) assumes the same.

## Working language

Client communication, transcripts, and product context are in **Brazilian Portuguese**. Keep user-facing strings, commit messages, and conversation with the user in pt-BR unless asked otherwise. Code identifiers stay in English.
