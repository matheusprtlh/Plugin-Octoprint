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
- **Multi-brand mandatory**: Bambu Lab, Creality, Elegoo, Prusa, "all printers". The plugin is brand-agnostic at the protocol layer (it just exposes a `brand` field). For Bambu specifically, see the caveat below.
- **Bambu Lab caveat**: Bambu printers do not register as OctoPrint serial printers (closed firmware). This plugin alone **cannot drive a Bambu**. To support Bambu, the user needs OctoPrint to first see the printer (which requires non-current firmware on Bambu's side, per the customer) — without that, Bambu is out of scope for this plugin and would need a sibling tool that talks Bambu's MQTT directly. The customer flagged this explicitly as needing study.

Budget anchor (for scope discussions only): R$ 500 was quoted for the bundled tasks (this plugin + an unrelated Anvisa/Light medical-prescription automation tracked elsewhere). Push back on scope creep that obviously exceeds this.

## Plugin (`plugin/`)

`OctoPrint-MarketplaceLink` v0.3.0, identifier `marketplace_link`, package `octoprint_marketplace_link`. Pure Python, depends only on `requests` (already shipped with OctoPrint).

Mixins used: `SettingsPlugin`, `AssetPlugin`, `TemplatePlugin`, `StartupPlugin`, `ShutdownPlugin`, `SimpleApiPlugin`, `EventHandlerPlugin`. Outbound only — the plugin never serves traffic.

### What it does

On any of these OctoPrint events — `Connected`, `Disconnected`, `PrintStarted`, `PrintDone`, `PrintFailed`, `PrintCancelled`, `PrintPaused`, `PrintResumed`, `Error` — the plugin POSTs JSON to the configured webhook URL. Optionally, a periodic `heartbeat` event fires every N minutes (default 5; 0 disables) so the receiving site can detect dead printers without waiting for a print event.

### Configuration

Settings page (Settings → Marketplace Link) takes:

- `webhook_url` — full URL receiving the POSTs.
- `auth_token` — optional Bearer token, sent in `Authorization: Bearer <token>`. Restricted to admin scope via `get_settings_restricted_paths()` so non-admins can't read it through OctoPrint's settings API.
- `brand` — Creality / Elegoo / Prusa / Bambu Lab / other.
- `model` — free text (e.g. "Ender 3 V2").
- `heartbeat_minutes` — int, 0–60. 0 disables.

A "Testar webhook" button POSTs a synthetic `event: "test"` payload immediately and reports the HTTP status; this path skips persisting the result so a manual test failure doesn't pollute the live delivery telemetry.

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

### Plugin dev install

OctoPrint plugins must run inside the same Python venv as OctoPrint itself. OctoPrint supports Python 3.9–3.12 (do not use 3.13).

```bash
python -m venv ~/octoprint-dev && source ~/octoprint-dev/bin/activate
pip install OctoPrint
pip install -e plugin/                 # editable install of our plugin
octoprint serve                        # opens http://localhost:5000
```

In the OctoPrint UI: Settings → Marketplace Link → fill webhook URL + token → "Testar webhook". Watch `octoprint.log` for `Marketplace Link starting` and any delivery warnings.

There are no plugin unit tests — testing OctoPrint plugins in isolation requires mocking `octoprint.plugin` heavily. Validate via the running OctoPrint instance against any HTTP echo service (e.g. webhook.site) for end-to-end checks.

### Deployment / distribution

Three install paths for end users, in increasing order of polish:

1. **GitHub-zip URL (fastest path to v0).** Tag a release on the plugin repo, then users go to `Settings → Plugin Manager → Get More → ...from URL` and paste `https://github.com/<org>/OctoPrint-MarketplaceLink/archive/v0.3.0.zip`. OctoPrint runs `pip install <url>` inside its venv and prompts a restart.
2. **PyPI.** `python -m build && twine upload dist/*` from `plugin/`. Users paste the package name `OctoPrint-MarketplaceLink` into Plugin Manager.
3. **Official Plugin Repository (long-term).** Submit a PR to https://github.com/OctoPrint/plugins.octoprint.org adding a YAML metadata file. Once merged, the plugin shows up in `Plugin Manager → Browse` with no URL needed. Requires a public GitHub repo, a working README, screenshots, and a pinned release.

### Plugin auto-update

`get_update_information()` declares a `github_release` source for OctoPrint's `softwareupdate` plugin. While the class constants `GITHUB_USER` / `GITHUB_REPO` still hold `"TODO-org"` / `"OctoPrint-MarketplaceLink"` it returns `{}`, so OctoPrint won't poll a non-existent repo. Replace those constants with the real coordinates before the first release.

## Working language

Client communication, transcripts, and product context are in **Brazilian Portuguese**. Keep user-facing strings, commit messages, and conversation with the user in pt-BR unless asked otherwise. Code identifiers stay in English.
