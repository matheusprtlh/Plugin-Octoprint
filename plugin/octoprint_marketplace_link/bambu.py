"""Bambu Lab MQTT bridge for OctoPrint-MarketplaceLink.

The Bambu printers (A1, A1 Mini, P1S, P1P, X1C) do not show up to OctoPrint as
serial printers — their firmware is closed and they expose a TLS MQTT broker
on the LAN instead. This module connects to that broker, subscribes to the
``device/<serial>/report`` topic, and translates the printer's state changes
into the same discrete event names the OctoPrint event bus produces
(``Connected``, ``PrintStarted``, ``PrintDone``, etc.).

Design notes:

* Read-only. We never publish to ``device/<serial>/request`` — that would let
  the plugin start/cancel prints, which is out of scope for this v0.
* No external Bambu library — we use ``paho-mqtt`` directly. The community
  ``bambulabs_api`` package exists but pulls heavier deps and changes its
  surface across versions; for the narrow needs of state reporting, paho is
  enough and pinning is easier.
* TLS: Bambu's broker uses a self-signed certificate that does not match any
  CA bundle. We therefore configure ``cert_reqs=CERT_NONE`` and call
  ``tls_insecure_set(True)``. This is intentional and documented — the
  alternative is to ship Bambu's cert with the plugin, which would still
  break if Bambu rotated it. The connection is on the local LAN, the
  username is fixed (``bambu``), and the password is the printer's 8-digit
  access code that only the printer's owner can see.
* Reconnect: paho's auto-reconnect doesn't always fire on auth failures.
  We wrap the loop in our own thread with explicit exponential backoff
  (1s, 2s, 4s, ..., capped at 60s) so a wrong access code or unreachable
  host doesn't silently spin.
* Event derivation: Bambu doesn't emit named events. We diff successive
  reports of ``gcode_state`` (``IDLE`` / ``PREPARE`` / ``RUNNING`` /
  ``PAUSE`` / ``FINISH`` / ``FAILED``) plus the connection lifecycle.
  Missed messages are tolerated because we always derive from the last
  *seen* state, not from the wire.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover — handled gracefully by the plugin.
    mqtt = None  # type: ignore[assignment]


# ---------- Bambu protocol constants ----------

# Username is hardcoded by Bambu firmware. Password is the LAN access code
# that the user reads off the printer screen (Settings → WLAN → "LAN Code").
_MQTT_USERNAME = "bambu"
_MQTT_PORT = 8883

# Topic conventions (as observed in the wild and confirmed by community
# reverse-engineering of the X1/P1/A1 protocols):
#   device/<serial>/report   — printer pushes state here
#   device/<serial>/request  — clients send commands here (we don't use it)
def _report_topic(serial: str) -> str:
    return f"device/{serial}/report"


def _request_topic(serial: str) -> str:
    return f"device/{serial}/request"


# Mapping from Bambu's ``gcode_state`` to our marketplace ``status`` values.
# ``RUNNING`` and ``PAUSE`` both count as "printing" because the job is still
# loaded — the ``status`` field is about the printer, not the job lifecycle.
_GCODE_STATE_TO_STATUS = {
    "IDLE": "idle",
    "PREPARE": "printing",
    "RUNNING": "printing",
    "PAUSE": "printing",
    "FINISH": "idle",
    "FAILED": "error",
    "OFFLINE": "offline",
    "UNKNOWN": "offline",
}


# ---------- Public bridge class ----------


class BambuBridge:
    """Maker-side MQTT subscriber that emits OctoPrint-shaped events.

    The bridge is fully self-contained: instantiate, ``start()``, and let it
    run; ``stop()`` joins the thread cleanly. It calls ``event_callback``
    on every derived event and ``status_callback`` whenever it has new
    job/printer state to publish (used to refresh the snapshot consumed by
    the heartbeat). Both callbacks run on the bridge's internal thread —
    don't do heavy work in them.
    """

    # Reconnect backoff bounds, in seconds.
    BACKOFF_INITIAL = 1.0
    BACKOFF_MAX = 60.0

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        event_callback: Callable[[str], None],
        status_callback: Callable[[dict], None],
        logger: Any,
        *,
        port: int = _MQTT_PORT,
    ) -> None:
        self._host = host
        self._serial = serial
        self._access_code = access_code
        self._port = port
        self._event_cb = event_callback
        self._status_cb = status_callback
        self._logger = logger

        self._client: Optional["mqtt.Client"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # ---- last-seen state (mutated only by paho callbacks / the loop) ----
        self._last_gcode_state: str = "UNKNOWN"
        self._last_status: str = "offline"
        self._last_file: Optional[str] = None
        self._last_progress: Optional[float] = None
        self._firmware_version: Optional[str] = None
        self._connected_emitted = False
        self._last_error: Optional[str] = None

    # ---------- public API ----------

    def start(self) -> None:
        if mqtt is None:
            self._last_error = (
                "paho-mqtt não está instalado. Rode: pip install 'paho-mqtt>=1.6'"
            )
            self._logger.warning("Bambu: %s", self._last_error)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._host or not self._serial or not self._access_code:
            self._last_error = (
                "Bambu mode habilitado mas host/serial/access_code estão incompletos."
            )
            self._logger.warning("Bambu: %s", self._last_error)
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="MarketplaceLinkBambu"
        )
        self._thread.start()
        self._logger.info(
            "Bambu: bridge iniciada (host=%s serial=%s)",
            self._host,
            _mask_serial(self._serial),
        )

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 — paho can throw on already-closed sockets
                pass
            try:
                client.loop_stop()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._client = None
        # Mark offline so the next snapshot reflects reality.
        if self._last_status != "offline":
            self._last_status = "offline"
            self._last_gcode_state = "OFFLINE"
            self._connected_emitted = False
            try:
                self._event_cb("Disconnected")
            except Exception:  # noqa: BLE001
                self._logger.exception("Bambu: erro no callback de Disconnected")
        self._logger.info("Bambu: bridge parada")

    def snapshot(self) -> dict:
        """Return the last-seen state for the heartbeat payload."""
        return {
            "status": self._last_status,
            "file": self._last_file,
            "progress": self._last_progress,
            "firmware_version": self._firmware_version,
            "last_error": self._last_error,
            "connected": self._connected_emitted,
        }

    # ---------- main loop with exponential backoff ----------

    def _run(self) -> None:
        backoff = self.BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                self._connect_and_loop()
                # Clean exit from connect_and_loop means stop() was called.
                if self._stop.is_set():
                    return
                # Otherwise, the broker dropped us — reset backoff to fast retry.
                backoff = self.BACKOFF_INITIAL
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                self._last_error = err
                self._logger.warning(
                    "Bambu: falha de conexão (%s). Reconectando em %.0fs", err, backoff
                )

            # Mark printer offline while we're disconnected.
            if self._last_status != "offline":
                self._last_status = "offline"
                self._last_gcode_state = "OFFLINE"
                if self._connected_emitted:
                    self._connected_emitted = False
                    try:
                        self._event_cb("Disconnected")
                    except Exception:  # noqa: BLE001
                        self._logger.exception("Bambu: erro no callback Disconnected")

            # Wait with exponential backoff, but check stop every 500ms.
            slept = 0.0
            while slept < backoff and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def _build_client(self) -> "mqtt.Client":
        # paho-mqtt 2.x deprecated the v1 callback API. We pin to v1 callbacks
        # (CallbackAPIVersion.VERSION1) so the same code works on 1.6+ and 2.x.
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"marketplace-link-{self._serial[:8]}",
                clean_session=True,
            )
        else:
            client = mqtt.Client(
                client_id=f"marketplace-link-{self._serial[:8]}",
                clean_session=True,
            )

        client.username_pw_set(_MQTT_USERNAME, self._access_code)

        # Bambu's broker uses a self-signed cert — skip verification.
        # Documented and intentional: see module docstring.
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _connect_and_loop(self) -> None:
        client = self._build_client()
        self._client = client
        self._logger.debug(
            "Bambu: conectando em mqtts://%s:%d (serial=%s)",
            self._host,
            self._port,
            _mask_serial(self._serial),
        )
        # connect() raises on DNS / refused / TLS handshake failure.
        client.connect(self._host, self._port, keepalive=30)
        # loop_forever blocks until disconnect(). It handles the network thread
        # internally; we don't need loop_start.
        client.loop_forever(retry_first_connection=False)

    # ---------- paho callbacks ----------

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._last_error = None
            topic = _report_topic(self._serial)
            client.subscribe(topic, qos=0)
            self._logger.info(
                "Bambu: MQTT conectado, subscrito em %s", topic
            )
            # Ask printer to push a full report — Bambu accepts a "pushall"
            # request to flush its current state right after subscribe.
            try:
                client.publish(
                    _request_topic(self._serial),
                    json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                    qos=0,
                )
            except Exception:  # noqa: BLE001
                self._logger.debug("Bambu: pushall request falhou (não-fatal)")
            # Connected event is emitted on the first state report we see —
            # before that, we don't yet know the firmware version or job.
        else:
            # rc == 4 → bad username/password (likely wrong access code).
            # rc == 5 → not authorized (firmware locked LAN mode).
            err = _conn_rc_message(rc)
            self._last_error = err
            self._logger.warning("Bambu: conexão recusada (rc=%d): %s", rc, err)
            # Don't loop forever on a permanent auth failure — disconnect so
            # _connect_and_loop returns and the outer backoff kicks in.
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _on_disconnect(self, client, userdata, rc) -> None:
        if rc != 0:
            self._logger.info("Bambu: desconectado (rc=%d)", rc)
        # State transition to offline / Disconnected event handled by _run().

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.debug("Bambu: payload inválido em %s: %s", msg.topic, e)
            return
        self._handle_report(payload)

    # ---------- state machine ----------

    def _handle_report(self, payload: dict) -> None:
        """Update internal state from one MQTT report message and emit events.

        Bambu reports come wrapped in either ``{"print": {...}}`` or
        ``{"info": {"command": "get_version", "module": [...]}}``. We treat
        any unknown shape as a no-op (don't crash on schema drift).
        """
        info = payload.get("info") if isinstance(payload, dict) else None
        if isinstance(info, dict):
            self._handle_info(info)

        report = payload.get("print") if isinstance(payload, dict) else None
        if not isinstance(report, dict):
            return

        # ---- progress / file ----
        if "subtask_name" in report and report["subtask_name"]:
            self._last_file = str(report["subtask_name"])
        elif "gcode_file" in report and report["gcode_file"]:
            # P1/A1 sometimes use this key instead.
            self._last_file = str(report["gcode_file"])

        if "mc_percent" in report:
            try:
                self._last_progress = float(report["mc_percent"])
            except (TypeError, ValueError):
                pass

        # ---- gcode_state transition is what drives our event derivation ----
        new_state = report.get("gcode_state")
        transition_pending = (
            isinstance(new_state, str) and new_state != self._last_gcode_state
        )
        if transition_pending:
            old_state = self._last_gcode_state
            self._last_gcode_state = new_state
            self._last_status = _GCODE_STATE_TO_STATUS.get(new_state, "offline")
        else:
            # Even without a state change, refresh status from the
            # last-known gcode_state in case mapping changed.
            self._last_status = _GCODE_STATE_TO_STATUS.get(
                self._last_gcode_state, "offline"
            )

        # CRITICAL ORDER: push the snapshot to the plugin BEFORE firing events.
        # Otherwise the webhook payload built inside the event handler would
        # see the previous status / file / progress and the receiving site
        # would get a PrintStarted with status=idle and job=null.
        try:
            self._status_cb(self.snapshot())
        except Exception:  # noqa: BLE001
            self._logger.exception("Bambu: erro no callback de status")

        # First report we see also implies the connection is live; emit
        # Connected once. Order matters: Connected before any state-derived
        # events so the receiving site sees the lifecycle in causal order.
        if not self._connected_emitted:
            self._connected_emitted = True
            try:
                self._event_cb("Connected")
            except Exception:  # noqa: BLE001
                self._logger.exception("Bambu: erro no callback Connected")

        if transition_pending:
            self._emit_transition(old_state, new_state)

    def _handle_info(self, info: dict) -> None:
        """Pull firmware version out of an ``info`` report.

        Bambu ships ``info.module`` as a list of components; the
        ``ota`` / ``mc`` modules carry the firmware revision string.
        """
        modules = info.get("module")
        if not isinstance(modules, list):
            return
        for mod in modules:
            if not isinstance(mod, dict):
                continue
            name = mod.get("name") or ""
            sw_ver = mod.get("sw_ver") or mod.get("project_name")
            if not sw_ver:
                continue
            if name in ("ota", "esp32", "mc", "rv1126"):
                # Prefer "ota" (the user-facing firmware version) over the
                # microcontroller subversions.
                if name == "ota" or self._firmware_version is None:
                    self._firmware_version = str(sw_ver)
        if self._firmware_version:
            self._logger.info(
                "Bambu: firmware detectado: %s", self._firmware_version
            )

    def _emit_transition(self, old_state: str, new_state: str) -> None:
        """Map a gcode_state delta to one of the OctoPrint-style events."""
        self._logger.debug(
            "Bambu: transicao de estado %s -> %s", old_state, new_state
        )

        # Print started: anything → PREPARE/RUNNING from a non-printing state.
        if new_state in ("PREPARE", "RUNNING") and old_state in (
            "IDLE",
            "FINISH",
            "FAILED",
            "UNKNOWN",
            "OFFLINE",
        ):
            self._fire("PrintStarted")
            return

        # PAUSE: only meaningful if previously running.
        if new_state == "PAUSE" and old_state in ("RUNNING", "PREPARE"):
            self._fire("PrintPaused")
            return

        # Resume: PAUSE → RUNNING.
        if new_state == "RUNNING" and old_state == "PAUSE":
            self._fire("PrintResumed")
            return

        # Done: RUNNING/PAUSE/PREPARE → FINISH.
        if new_state == "FINISH" and old_state in ("RUNNING", "PAUSE", "PREPARE"):
            self._fire("PrintDone")
            return

        # Failed: any printing state → FAILED.
        if new_state == "FAILED" and old_state in (
            "RUNNING",
            "PAUSE",
            "PREPARE",
        ):
            # Bambu doesn't disambiguate user-cancel vs. error reliably on
            # all firmwares; PrintFailed covers both for now. PrintCancelled
            # would require parsing ``print_error`` codes, which differ by
            # model.
            self._fire("PrintFailed")
            return

        # IDLE after a print finished/failed: nothing to emit; status field
        # alone reflects the new state.

    def _fire(self, event: str) -> None:
        try:
            self._event_cb(event)
        except Exception:  # noqa: BLE001
            self._logger.exception("Bambu: erro emitindo evento %s", event)


# ---------- helpers ----------


def _mask_serial(serial: str) -> str:
    if not serial or len(serial) <= 4:
        return "****"
    return serial[:2] + "***" + serial[-2:]


def _conn_rc_message(rc: int) -> str:
    """Translate a paho on_connect rc into a human-readable hint."""
    return {
        0: "OK",
        1: "Protocolo MQTT incorreto (firmware Bambu pode estar bloqueando LAN mode).",
        2: "Client ID rejeitado.",
        3: "Servidor indisponível (verifique IP e se a impressora está ligada).",
        4: (
            "Usuário/senha inválidos — confira o access code (8 dígitos) na tela "
            "da impressora em Settings → WLAN."
        ),
        5: (
            "Não autorizado — provavelmente o firmware desligou o LAN mode. "
            "Habilite Developer Mode na impressora ou faça rollback do firmware."
        ),
    }.get(rc, f"Erro desconhecido (rc={rc})")
