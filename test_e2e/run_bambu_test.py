"""End-to-end test for Bambu mode of OctoPrint-MarketplaceLink.

We don't have a real Bambu printer on the dev machine, and standing up a
real TLS MQTT broker that speaks Bambu's flavor is overkill for what we
want to verify. The actual logic that turns Bambu reports into webhook
events lives in ``BambuBridge._handle_report`` — a pure state machine
that diffs successive ``gcode_state`` values.

So this test:

1. Imports the plugin against the same stubs ``run_test.py`` uses.
2. Configures Bambu mode + the live ``webhook_receiver.py`` URL.
3. Bypasses the MQTT thread entirely — it instantiates ``BambuBridge``
   directly, wires it to the plugin's callbacks, then feeds it a
   handcrafted sequence of Bambu-shaped report payloads simulating an
   idle → starting → printing → 50% → done → idle lifecycle.
4. Verifies that the right webhook events arrive at ``received.log``
   in the right order, with ``brand=bambu``, ``firmware_version`` set,
   and the job file/progress propagating correctly.

Usage (with ``webhook_receiver.py`` running in another terminal):

    python test_e2e/webhook_receiver.py --reset-log     # terminal 1
    python test_e2e/run_bambu_test.py                   # terminal 2
"""

import json
import sys
import time
from pathlib import Path

# Same stub injection as run_test.py.
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import os as _os
if _os.environ.get("PLUGIN_USE_INSTALLED") != "1":
    sys.path.insert(0, str(HERE.parent / "plugin"))

RECEIVED_LOG = HERE / "received.log"


# ============================================================
# Reuse the mocks from run_test.py
# ============================================================

from run_test import _Settings, _Printer, _Logger, _read_log, _wait_for_deliveries  # noqa: E402


# ============================================================
# Synthetic Bambu report payloads
# ============================================================

# Firmware version probe — Bambu sends this in response to a "get_version"
# request, but we replay it once at startup so the plugin learns the
# firmware string.
INFO_FIRMWARE = {
    "info": {
        "command": "get_version",
        "module": [
            {"name": "ota", "sw_ver": "01.06.00.00"},
            {"name": "esp32", "sw_ver": "01.05.00.00"},
        ],
    }
}

# State reports. Each is what the printer would publish to
# device/<serial>/report at the corresponding lifecycle moment.
def report(gcode_state, percent=None, file_=None, extra=None):
    payload = {"print": {"gcode_state": gcode_state}}
    if percent is not None:
        payload["print"]["mc_percent"] = percent
    if file_ is not None:
        payload["print"]["subtask_name"] = file_
    if extra:
        payload["print"].update(extra)
    return payload


# ============================================================
# Bootstrap
# ============================================================

def _instantiate_plugin(webhook_url, token):
    from octoprint_marketplace_link import MarketplaceLinkPlugin  # type: ignore

    plugin = MarketplaceLinkPlugin()
    defaults = plugin.get_settings_defaults()
    defaults.update(
        {
            "webhook_url": webhook_url,
            "auth_token": token,
            "brand": "other",  # intentionally NOT bambu — should be overridden
            "model": "",
            "heartbeat_minutes": 0,
            # Bambu mode on:
            "bambu_enabled": True,
            "bambu_host": "192.168.0.123",
            "bambu_serial": "01PS00A0B0000001",
            "bambu_access_code": "12345678",
            "bambu_model": "P1S",
        }
    )
    plugin._settings = _Settings(defaults)
    plugin._printer = _Printer()
    plugin._logger = _Logger()
    plugin._plugin_version = "0.4.0"
    return plugin


def _build_bridge_for(plugin):
    """Construct a BambuBridge wired to the plugin, but never started.

    We bypass start() / the MQTT thread / the TLS handshake — we want to
    exercise the state machine, not paho-mqtt.
    """
    from octoprint_marketplace_link.bambu import BambuBridge

    bridge = BambuBridge(
        host="192.168.0.123",
        serial="01PS00A0B0000001",
        access_code="12345678",
        event_callback=plugin._on_bambu_event,
        status_callback=plugin._on_bambu_status,
        logger=plugin._logger,
    )
    return bridge


def main():
    if RECEIVED_LOG.exists():
        RECEIVED_LOG.unlink()

    print("=== E2E test do plugin OctoPrint-MarketplaceLink (modo Bambu) ===\n")

    plugin = _instantiate_plugin(
        webhook_url="http://127.0.0.1:9000/incoming",
        token="bambu-test-token-xyz",
    )

    print("[harness] startup (sem disparar a thread MQTT real)")
    # Don't call on_after_startup() — that would try to TLS-connect to a
    # nonexistent printer. Build the bridge manually and inject reports.
    bridge = _build_bridge_for(plugin)

    # ---- Synthetic lifecycle ----
    # Each entry: (description, payload_to_inject)
    sequence = [
        ("firmware probe (info.module)", INFO_FIRMWARE),
        ("idle report", report("IDLE")),                                # → Connected
        ("prepare", report("PREPARE", file_="benchy.3mf", percent=0)),  # → PrintStarted
        ("running 25%", report("RUNNING", file_="benchy.3mf", percent=25)),
        ("running 50%", report("RUNNING", file_="benchy.3mf", percent=50)),
        ("paused", report("PAUSE", file_="benchy.3mf", percent=50)),    # → PrintPaused
        ("resumed", report("RUNNING", file_="benchy.3mf", percent=55)), # → PrintResumed
        ("running 99%", report("RUNNING", file_="benchy.3mf", percent=99)),
        ("finished", report("FINISH", file_="benchy.3mf", percent=100)),# → PrintDone
        ("back to idle", report("IDLE")),                               # no event
    ]

    expected_events = [
        "Connected",
        "PrintStarted",
        "PrintPaused",
        "PrintResumed",
        "PrintDone",
    ]

    print(f"[harness] injetando {len(sequence)} mensagens MQTT sintéticas...\n")
    for desc, payload in sequence:
        print(f"[harness] -> {desc}")
        bridge._handle_report(payload)
        time.sleep(0.3)  # let the daemon thread post

    # Now simulate a printer-side failure on a fresh job to exercise PrintFailed.
    print("\n[harness] simulando uma falha de impressão...")
    fail_seq = [
        ("new prepare", report("PREPARE", file_="failed.3mf", percent=0)),  # PrintStarted
        ("running", report("RUNNING", file_="failed.3mf", percent=10)),
        ("failed", report("FAILED", file_="failed.3mf", percent=10)),  # PrintFailed
    ]
    for desc, payload in fail_seq:
        print(f"[harness] -> {desc}")
        bridge._handle_report(payload)
        time.sleep(0.3)

    expected_events += ["PrintStarted", "PrintFailed"]

    # ---- Wait & verify ----
    expected = len(expected_events)
    print(f"\n[harness] aguardando até {expected} entregas no receiver...")
    records = _wait_for_deliveries(expected, timeout_s=10)
    print(f"[harness] {len(records)}/{expected} entregas registradas em received.log")

    # ---- Disconnect simulation ----
    print("\n[harness] simulando disconnect (stop do bridge)...")
    bridge.stop()
    time.sleep(0.5)

    # ---- Report-derived snapshot ----
    snap = bridge.snapshot()
    print(f"\n[harness] snapshot final: {snap}")

    # ---- Validate the payload contents ----
    print("\n=== Validação ===")
    records = _read_log()
    actual_events = [(r.get("payload") or {}).get("event") for r in records]
    print(f"  Eventos esperados: {expected_events}")
    print(f"  Eventos recebidos: {actual_events}")

    rc = 0

    # Disconnected may or may not arrive depending on stop() timing — accept
    # either prefix-match or full match.
    if actual_events[: len(expected_events)] != expected_events:
        print("  [FAIL] sequência de eventos não bate com o esperado")
        rc = 1
    else:
        print("  [OK] sequência de eventos correta")

    # Spot-check the PrintStarted payload.
    started = next(
        (r["payload"] for r in records if r["payload"]["event"] == "PrintStarted"), None
    )
    if started is None:
        print("  [FAIL] não achei PrintStarted no log")
        rc = 1
    else:
        printer = started.get("printer") or {}
        if printer.get("brand") != "bambu":
            print(f"  [FAIL] brand esperado 'bambu', veio {printer.get('brand')!r}")
            rc = 1
        else:
            print("  [OK] printer.brand == 'bambu' (sobrescreveu o dropdown)")
        if printer.get("model") != "P1S":
            print(f"  [FAIL] model esperado 'P1S', veio {printer.get('model')!r}")
            rc = 1
        else:
            print("  [OK] printer.model == 'P1S' (do bambu_model)")
        if printer.get("firmware_version") != "01.06.00.00":
            print(
                "  [FAIL] firmware_version esperado '01.06.00.00', "
                f"veio {printer.get('firmware_version')!r}"
            )
            rc = 1
        else:
            print("  [OK] firmware_version detectado a partir de info.module")

        if started.get("status") != "printing":
            print(f"  [FAIL] status esperado 'printing', veio {started.get('status')!r}")
            rc = 1
        else:
            print("  [OK] status == 'printing'")

        job = started.get("job") or {}
        if job.get("file") != "benchy.3mf":
            print(f"  [FAIL] job.file esperado 'benchy.3mf', veio {job.get('file')!r}")
            rc = 1
        else:
            print("  [OK] job.file extraído de subtask_name")

    # Spot-check schema_version is unchanged.
    if records and records[0]["payload"].get("schema_version") != 1:
        print(
            "  [FAIL] schema_version mudou: "
            f"{records[0]['payload'].get('schema_version')}"
        )
        rc = 1
    else:
        print("  [OK] schema_version == 1 (sem regressão)")

    # Bearer header still applied.
    if records and "Bearer" not in (records[0].get("auth_header") or ""):
        print(f"  [FAIL] header sem Bearer: {records[0].get('auth_header')!r}")
        rc = 1
    else:
        print("  [OK] header Authorization Bearer preservado")

    print("\n=== Resumo do received.log ===")
    print(f"  Total de POSTs: {len(records)}")
    print(f"  Eventos em ordem: {actual_events}")
    print("\n  Exemplo de payload (primeiro PrintStarted):")
    if started:
        print(json.dumps(started, ensure_ascii=False, indent=2))

    return rc


if __name__ == "__main__":
    sys.exit(main())
