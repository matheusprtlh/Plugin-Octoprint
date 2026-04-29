import threading
from datetime import datetime, timezone

import requests

import octoprint.plugin
from octoprint.util import RepeatedTimer


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
        }

    def get_settings_restricted_paths(self):
        # auth_token is a bearer credential — never expose it to non-admins.
        return {"admin": [["auth_token"]]}

    def on_settings_save(self, data):
        old_interval = self._settings.get_int(["heartbeat_minutes"])
        result = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        new_interval = self._settings.get_int(["heartbeat_minutes"])
        if old_interval != new_interval:
            self._restart_heartbeat()
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

    # ---- Startup / Shutdown ----

    def on_after_startup(self):
        self._logger.info("Marketplace Link starting (webhook mode)")
        self._restart_heartbeat()

    def on_shutdown(self):
        self._stop_heartbeat()

    # ---- EventHandlerPlugin ----

    def on_event(self, event, payload):
        if event in self._PRINT_EVENTS:
            self._post_async(event=event)

    # ---- SimpleApiPlugin ----

    def get_api_commands(self):
        return {"test_webhook": []}

    def on_api_command(self, command, data):
        from flask import abort, jsonify

        if command != "test_webhook":
            abort(400)
        ok, status_code, error = self._post(event="test", record_failure=False)
        return jsonify({"ok": ok, "status": status_code, "error": error or ""})

    def on_api_get(self, request):
        from flask import jsonify

        return jsonify(
            {
                "configured": bool(self._webhook_url()),
                "last_delivery_at": self._settings.get(["last_delivery_at"]),
                "last_delivery_status": self._settings.get_int(["last_delivery_status"]),
                "last_delivery_error": self._settings.get(["last_delivery_error"]),
            }
        )

    # ---- internals: settings accessors ----

    def _webhook_url(self):
        return (self._settings.get(["webhook_url"]) or "").strip()

    def _auth_token(self):
        return (self._settings.get(["auth_token"]) or "").strip()

    # ---- internals: state detection ----

    def _detect_firmware(self):
        try:
            info = self._printer.firmware_info
        except Exception:
            return None
        if isinstance(info, dict):
            return info.get("name") or info.get("FIRMWARE_NAME")
        return None

    def _current_status(self):
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

    # ---- internals: webhook ----

    def _build_payload(self, event):
        printer_name = self._settings.global_get(["appearance", "name"]) or "OctoPrint"
        return {
            "schema_version": self.PAYLOAD_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
            "status": self._current_status(),
            "printer": {
                "name": printer_name,
                "brand": self._settings.get(["brand"]) or "other",
                "model": self._settings.get(["model"]) or None,
                "firmware_version": self._detect_firmware(),
            },
            "job": self._current_job() if self._current_status() == "printing" else None,
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
__plugin_version__ = "0.3.0"
__plugin_description__ = "Posta status da impressora num webhook configurável a cada evento do OctoPrint."
__plugin_implementation__ = MarketplaceLinkPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
}
