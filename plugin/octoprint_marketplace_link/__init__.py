import threading
from datetime import datetime, timezone

import requests

import octoprint.plugin
from octoprint.util import RepeatedTimer

from .bambu import BambuBridge


class MarketplaceLinkPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    """Posts printer state to a configurable webhook URL on OctoPrint events.

    There is no central server in this plugin's contract: the user configures
    the URL of whichever site they want notified, and the plugin POSTs JSON
    payloads on relevant printer events. An optional bearer token authorizes
    the request; the receiving site decides what to do with the data.

    Two state sources are supported:

    1. **Serial mode** — the default. Listens to OctoPrint's event bus
       (``Connected``, ``PrintStarted``, etc.) and pulls live job state from
       ``self._printer``. Works for any printer OctoPrint can talk to over
       serial (Marlin / Klipper / RepRap).

    2. **Bambu mode** — opt-in via ``bambu_enabled``. Bambu Lab printers
       (A1 / A1 Mini / P1S / P1P / X1C) don't show up on serial because the
       firmware is closed; instead, we connect directly to the printer's
       local TLS MQTT broker on port 8883, subscribe to its state topic,
       and translate the printer's ``gcode_state`` transitions into the same
       event names the serial path emits. Same payload shape, same webhook,
       same telemetry — the receiving site can't tell the two modes apart.
    """

    HTTP_TIMEOUT = 10

    # Bump when the payload structure changes in a non-additive way (renames,
    # removals, type changes). Adding a new optional field does NOT bump.
    PAYLOAD_SCHEMA_VERSION = 1

    # Replace with the real GitHub coordinates before the first release.
    # While ``GITHUB_USER == "TODO-org"``, ``get_update_information()`` returns
    # ``{}`` so OctoPrint's softwareupdate plugin doesn't poll a non-existent
    # repo on every check.
    GITHUB_USER = "matheusprtlh"
    GITHUB_REPO = "Plugin-Octoprint"

    _PRINT_EVENTS = {
        "Connected",
        "Disconnected",
        "PrintStarted",
        "PrintDone",
        "PrintFailed",
        "PrintCancelled",
        "PrintPaused",
        "PrintResumed",
        "Error",
    }

    def __init__(self):
        super().__init__()
        self._heartbeat_timer = None
        self._send_lock = threading.Lock()
        self._bambu = None  # type: BambuBridge | None
        self._bambu_snapshot = {
            "status": "offline",
            "file": None,
            "progress": None,
            "firmware_version": None,
            "last_error": None,
            "connected": False,
        }

    # ---- SettingsPlugin ----

    def get_settings_defaults(self):
        return {
            "webhook_url": "",
            "auth_token": "",
            "brand": "other",
            "model": "",
            "heartbeat_minutes": 5,
            "last_delivery_at": "",
            "last_delivery_status": 0,
            "last_delivery_error": "",
            # ---- Bambu mode ----
            "bambu_enabled": False,
            "bambu_host": "",
            "bambu_serial": "",
            "bambu_access_code": "",
            "bambu_model": "",
        }

    def get_settings_restricted_paths(self):
        # auth_token is a bearer credential — never expose it to non-admins.
        # bambu_access_code is a printer-LAN credential — same treatment.
        return {
            "admin": [
                ["auth_token"],
                ["bambu_access_code"],
            ]
        }

    def on_settings_save(self, data):
        old_interval = self._settings.get_int(["heartbeat_minutes"])
        old_bambu_enabled = bool(self._settings.get(["bambu_enabled"]))
        old_bambu_sig = self._bambu_signature()

        result = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        new_interval = self._settings.get_int(["heartbeat_minutes"])
        new_bambu_enabled = bool(self._settings.get(["bambu_enabled"]))
        new_bambu_sig = self._bambu_signature()

        if old_interval != new_interval:
            self._restart_heartbeat()

        # Restart Bambu bridge if it was toggled or any connection param
        # changed. Cheap to do — stop() / start() are idempotent.
        if (old_bambu_enabled != new_bambu_enabled) or (old_bambu_sig != new_bambu_sig):
            self._restart_bambu()

        return result

    # ---- AssetPlugin ----

    def get_assets(self):
        return {
            "js": ["js/marketplace_link.js"],
            "css": ["css/marketplace_link.css"],
        }

    # ---- TemplatePlugin ----

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": True},
            {"type": "navbar", "custom_bindings": True},
        ]

    def is_template_autoescaped(self):
        # Our templates only use {{ _('...') }} (static i18n literals) and
        # Knockout data-bind attributes — never raw HTML interpolation. Safe
        # under autoescape, and forward-compatible with OctoPrint 1.13.
        return True

    # ---- SimpleApiPlugin protection ----

    def is_api_protected(self):
        # test_webhook fires a real HTTP POST to a configured URL; test_bambu
        # opens an outbound TLS connection to a printer. Both are state-changing
        # and must require an authenticated session. on_api_get returns
        # telemetry only (no secrets), but protecting the whole surface is
        # simpler and forward-compatible with the future OctoPrint default.
        return True

    # ---- Startup / Shutdown ----

    def on_after_startup(self):
        mode = "Bambu+webhook" if self._bambu_enabled() else "webhook"
        self._logger.info("Marketplace Link starting (%s mode)", mode)
        self._restart_heartbeat()
        if self._bambu_enabled():
            self._start_bambu()

    def on_shutdown(self):
        self._stop_heartbeat()
        self._stop_bambu()

    # ---- EventHandlerPlugin ----

    def on_event(self, event, payload):
        # Serial-mode events from OctoPrint's bus. In Bambu mode the printer
        # is invisible to OctoPrint anyway, so these never fire; events come
        # from the bridge instead via _on_bambu_event().
        if event in self._PRINT_EVENTS:
            self._post_async(event=event)

    # ---- SimpleApiPlugin ----

    def get_api_commands(self):
        return {
            "test_webhook": [],
            "test_bambu": [],
        }

    def on_api_command(self, command, data):
        from flask import abort, jsonify

        if command == "test_webhook":
            ok, status_code, error = self._post(event="test", record_failure=False)
            return jsonify({"ok": ok, "status": status_code, "error": error or ""})

        if command == "test_bambu":
            ok, error = self._test_bambu_connection()
            return jsonify({"ok": ok, "error": error or ""})

        abort(400)

    def on_api_get(self, request):
        from flask import jsonify

        return jsonify(
            {
                "configured": bool(self._webhook_url()),
                "last_delivery_at": self._settings.get(["last_delivery_at"]),
                "last_delivery_status": self._settings.get_int(["last_delivery_status"]),
                "last_delivery_error": self._settings.get(["last_delivery_error"]),
                "bambu_enabled": self._bambu_enabled(),
                "bambu_status": self._bambu_snapshot.get("status"),
                "bambu_firmware": self._bambu_snapshot.get("firmware_version"),
                "bambu_last_error": self._bambu_snapshot.get("last_error"),
            }
        )

    # ---- internals: settings accessors ----

    def _webhook_url(self):
        return (self._settings.get(["webhook_url"]) or "").strip()

    def _auth_token(self):
        return (self._settings.get(["auth_token"]) or "").strip()

    def _bambu_enabled(self):
        return bool(self._settings.get(["bambu_enabled"]))

    def _bambu_signature(self):
        """Tuple of all Bambu connection params, used to detect config changes."""
        return (
            (self._settings.get(["bambu_host"]) or "").strip(),
            (self._settings.get(["bambu_serial"]) or "").strip(),
            (self._settings.get(["bambu_access_code"]) or "").strip(),
        )

    # ---- internals: state detection (serial mode) ----

    def _detect_firmware(self):
        try:
            info = self._printer.firmware_info
        except Exception:
            return None
        if isinstance(info, dict):
            return info.get("name") or info.get("FIRMWARE_NAME")
        return None

    def _current_status(self):
        # Bambu mode short-circuits the OctoPrint serial probes — those would
        # always say "offline" because OctoPrint can't see the Bambu printer.
        if self._bambu_enabled():
            return self._bambu_snapshot.get("status") or "offline"
        try:
            if self._printer.is_printing():
                return "printing"
            if self._printer.is_error():
                return "error"
            if self._printer.is_operational():
                return "idle"
        except Exception:
            pass
        return "offline"

    def _current_job(self):
        if self._bambu_enabled():
            file_ = self._bambu_snapshot.get("file")
            progress = self._bambu_snapshot.get("progress")
            return {
                "file": file_,
                "progress": round(progress, 2) if isinstance(progress, (int, float)) else None,
            }

        try:
            data = self._printer.get_current_data()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        job = data.get("job") or {}
        progress = data.get("progress") or {}
        file_info = job.get("file") or {}
        completion = progress.get("completion")
        return {
            "file": file_info.get("name"),
            "progress": round(completion, 2) if isinstance(completion, (int, float)) else None,
        }

    def _resolved_brand(self):
        if self._bambu_enabled():
            # Bambu mode pins the brand regardless of the dropdown — the
            # receiving site needs to trust the field.
            return "bambu"
        return self._settings.get(["brand"]) or "other"

    def _resolved_model(self):
        if self._bambu_enabled():
            # Prefer the Bambu-specific model field, fall back to the generic
            # "model" if the user only filled one of them.
            bambu_model = (self._settings.get(["bambu_model"]) or "").strip()
            if bambu_model:
                return bambu_model
        return self._settings.get(["model"]) or None

    def _resolved_firmware(self):
        if self._bambu_enabled():
            return self._bambu_snapshot.get("firmware_version")
        return self._detect_firmware()

    # ---- internals: webhook ----

    def _build_payload(self, event):
        printer_name = self._settings.global_get(["appearance", "name"]) or "OctoPrint"
        status = self._current_status()
        return {
            "schema_version": self.PAYLOAD_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
            "status": status,
            "printer": {
                "name": printer_name,
                "brand": self._resolved_brand(),
                "model": self._resolved_model(),
                "firmware_version": self._resolved_firmware(),
            },
            "job": self._current_job() if status == "printing" else None,
        }

    def _post(self, event, record_failure=True):
        """Send one webhook POST. Returns ``(ok, status_code, error_or_None)``.

        Setting ``record_failure=False`` skips persisting the result to
        settings — used by the "test webhook" button so a manual test failure
        doesn't override the live delivery telemetry.
        """
        url = self._webhook_url()
        if not url:
            self._logger.debug("evento %s descartado: webhook_url não configurada", event)
            return False, 0, "webhook_url não configurada"

        headers = {"Content-Type": "application/json"}
        token = self._auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = self._build_payload(event)
        try:
            with self._send_lock:
                r = requests.post(url, json=payload, headers=headers, timeout=self.HTTP_TIMEOUT)
        except requests.RequestException as e:
            err = str(e)
            self._logger.warning("falha de rede ao postar %s: %s", event, err)
            if record_failure:
                self._record_delivery(0, err)
            return False, 0, err

        ok = 200 <= r.status_code < 300
        err = None if ok else (r.text[:200] if r.text else f"HTTP {r.status_code}")
        if ok:
            self._logger.debug("evento %s entregue (HTTP %d)", event, r.status_code)
        else:
            self._logger.warning(
                "webhook respondeu HTTP %d para evento %s: %s", r.status_code, event, err
            )
        if record_failure:
            self._record_delivery(r.status_code, err or "")
        return ok, r.status_code, err

    def _post_async(self, event):
        threading.Thread(
            target=self._post, args=(event,), daemon=True, name="MarketplaceLinkPost"
        ).start()

    def _record_delivery(self, status_code, error):
        self._settings.set(
            ["last_delivery_at"],
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        self._settings.set_int(["last_delivery_status"], int(status_code or 0))
        self._settings.set(["last_delivery_error"], error or "")
        self._settings.save()

    # ---- internals: heartbeat ----

    def _heartbeat_seconds(self):
        minutes = self._settings.get_int(["heartbeat_minutes"]) or 0
        return minutes * 60 if minutes > 0 else 0

    def _start_heartbeat(self):
        seconds = self._heartbeat_seconds()
        if seconds <= 0 or self._heartbeat_timer is not None:
            return
        if not self._webhook_url():
            return
        self._heartbeat_timer = RepeatedTimer(
            seconds, lambda: self._post(event="heartbeat"), run_first=False
        )
        self._heartbeat_timer.start()

    def _stop_heartbeat(self):
        if self._heartbeat_timer is not None:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None

    def _restart_heartbeat(self):
        self._stop_heartbeat()
        self._start_heartbeat()

    # ---- internals: Bambu bridge ----

    def _start_bambu(self):
        if self._bambu is not None:
            return
        host, serial, access_code = self._bambu_signature()
        self._bambu = BambuBridge(
            host=host,
            serial=serial,
            access_code=access_code,
            event_callback=self._on_bambu_event,
            status_callback=self._on_bambu_status,
            logger=self._logger,
        )
        self._bambu.start()
        # Surface a "Bambu started but unreachable" condition through the
        # delivery error field so the navbar widget reflects it. We re-check
        # after a short window — by then the bridge has either connected or
        # logged its first error.
        threading.Timer(8.0, self._check_bambu_health).start()

    def _stop_bambu(self):
        if self._bambu is None:
            return
        try:
            self._bambu.stop()
        finally:
            self._bambu = None
        self._bambu_snapshot = {
            "status": "offline",
            "file": None,
            "progress": None,
            "firmware_version": None,
            "last_error": None,
            "connected": False,
        }

    def _restart_bambu(self):
        self._stop_bambu()
        if self._bambu_enabled():
            self._start_bambu()

    def _on_bambu_event(self, event):
        # Funnel the bridge's derived events through the same async POST path
        # the serial mode uses. Identical webhook, identical telemetry.
        if event in self._PRINT_EVENTS:
            self._post_async(event=event)
        else:
            self._logger.debug("Bambu: evento ignorado (não suportado): %s", event)

    def _on_bambu_status(self, snapshot):
        self._bambu_snapshot = snapshot or self._bambu_snapshot

    def _check_bambu_health(self):
        if self._bambu is None:
            return
        snap = self._bambu.snapshot() if self._bambu else {}
        last_error = snap.get("last_error")
        if last_error and not snap.get("connected"):
            # Persist the error so navbar widget goes yellow without waiting
            # for a webhook attempt to fail.
            self._record_delivery(0, f"Bambu: {last_error}")

    def _test_bambu_connection(self):
        """Run a short-lived MQTT connection probe synchronously.

        Used by the "Testar conexão Bambu" button. Returns ``(ok, error)``.
        Does not touch the long-running bridge — it builds a transient client
        with the current settings and waits up to 8s for a CONNACK.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return False, "paho-mqtt não instalado. Reinstale o plugin."

        import ssl

        host, serial, access_code = self._bambu_signature()
        if not host or not serial or not access_code:
            return False, "Preencha host, serial e access code antes de testar."

        result = {"connected": False, "error": None}
        done = threading.Event()

        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"marketplace-link-test-{serial[:8]}",
            )
        else:
            client = mqtt.Client(client_id=f"marketplace-link-test-{serial[:8]}")
        client.username_pw_set("bambu", access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)

        def on_connect(c, u, f, rc):
            if rc == 0:
                result["connected"] = True
            else:
                from .bambu import _conn_rc_message
                result["error"] = _conn_rc_message(rc)
            try:
                c.disconnect()
            except Exception:
                pass
            done.set()

        client.on_connect = on_connect

        try:
            client.connect(host, 8883, keepalive=10)
        except Exception as e:
            return False, f"Falha de rede: {type(e).__name__}: {e}"

        client.loop_start()
        connected_in_time = done.wait(timeout=8.0)
        try:
            client.loop_stop()
        except Exception:
            pass

        if not connected_in_time:
            return False, "Timeout aguardando CONNACK (verifique IP/porta 8883)."
        if result["connected"]:
            return True, None
        return False, result["error"] or "Conexão recusada."

    # ---- softwareupdate hook ----

    def get_update_information(self):
        if self.GITHUB_USER == "TODO-org":
            return {}
        return {
            "marketplace_link": {
                "displayName": "Marketplace Link",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": self.GITHUB_USER,
                "repo": self.GITHUB_REPO,
                "current": self._plugin_version,
                "pip": (
                    f"https://github.com/{self.GITHUB_USER}/{self.GITHUB_REPO}"
                    "/archive/{target_version}.zip"
                ),
            }
        }


__plugin_name__ = "Marketplace Link"
__plugin_pythoncompat__ = ">=3.9,<3.13"
__plugin_version__ = "0.4.0"
__plugin_description__ = (
    "Posta status da impressora num webhook configurável a cada evento. "
    "Suporta impressoras serial (Marlin/Klipper/RepRap) e Bambu Lab via MQTT."
)
__plugin_implementation__ = MarketplaceLinkPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
}
