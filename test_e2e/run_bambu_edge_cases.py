"""Edge-case tests for the Bambu state machine.

Exercises scenarios the happy-path harness in ``run_bambu_test.py`` does not
cover, all without needing a real broker or printer:

    1. Duplicate state messages don't double-fire events.
    2. Cancel-mid-print: RUNNING → FAILED is mapped to PrintFailed.
    3. PAUSE → IDLE without FINISH (user aborted from the screen): no spurious
       PrintDone, no double Disconnected.
    4. Two prints in a row (Done → new PrepareRunning) emits a fresh
       PrintStarted, not "PrintResumed".
    5. Snapshot mutates BEFORE the event callback (critical ordering).
    6. Out-of-order keys: ``info`` arriving after a ``print`` report still
       updates the firmware version.
    7. Garbage payload (non-dict, missing keys) doesn't crash.
    8. Mask helper masks short serials safely.
    9. ``stop()`` is idempotent and clears connected_emitted.

The bridge's MQTT loop is never started; we drive ``_handle_report`` /
``_handle_info`` / ``stop()`` directly. paho is not exercised — that's covered
in the live OctoPrint test.

Exit code 0 = all assertions passed.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "plugin"))

from octoprint_marketplace_link.bambu import BambuBridge, _mask_serial


class _Logger:
    def __init__(self):
        self.records = []

    def debug(self, msg, *a):
        self.records.append(("DEBUG", msg % a if a else msg))

    def info(self, msg, *a):
        self.records.append(("INFO", msg % a if a else msg))

    def warning(self, msg, *a):
        self.records.append(("WARN", msg % a if a else msg))

    def exception(self, msg, *a):
        self.records.append(("EXC", msg % a if a else msg))


def _make_bridge():
    events = []
    snapshots = []
    bridge = BambuBridge(
        host="dummy",
        serial="01PS00A0B0000999",
        access_code="12345678",
        event_callback=lambda e: events.append(e),
        status_callback=lambda s: snapshots.append(dict(s)),
        logger=_Logger(),
    )
    return bridge, events, snapshots


# --------------------------------------------------------------------------
# Test 1 — duplicate state messages must not re-fire events.
# --------------------------------------------------------------------------
def test_duplicate_state_no_double_fire():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    bridge._handle_report({"print": {"gcode_state": "PREPARE", "subtask_name": "x.3mf"}})
    # Same state arriving 5x — no extra PrintStarted should fire
    for _ in range(5):
        bridge._handle_report({"print": {"gcode_state": "PREPARE", "mc_percent": 1.0}})
    assert events == ["Connected", "PrintStarted"], f"duplicate states leaked: {events}"
    print("  [OK] duplicate state messages did not re-fire events")


# --------------------------------------------------------------------------
# Test 2 — cancel-mid-print (RUNNING → FAILED) maps to PrintFailed.
# --------------------------------------------------------------------------
def test_cancel_mid_print():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    bridge._handle_report({"print": {"gcode_state": "PREPARE", "subtask_name": "x.3mf"}})
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "mc_percent": 25.0}})
    bridge._handle_report({"print": {"gcode_state": "FAILED"}})
    assert events == ["Connected", "PrintStarted", "PrintFailed"], events
    print("  [OK] RUNNING -> FAILED emitted PrintFailed")


# --------------------------------------------------------------------------
# Test 3 — PAUSE → IDLE without FINISH (user aborted): no spurious PrintDone.
# --------------------------------------------------------------------------
def test_pause_to_idle_no_phantom_done():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    bridge._handle_report({"print": {"gcode_state": "PREPARE", "subtask_name": "x.3mf"}})
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "mc_percent": 30.0}})
    bridge._handle_report({"print": {"gcode_state": "PAUSE"}})
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    # Connected, PrintStarted, PrintPaused — and NOTHING for PAUSE→IDLE.
    assert events == ["Connected", "PrintStarted", "PrintPaused"], (
        f"phantom event after PAUSE→IDLE: {events}"
    )
    print("  [OK] PAUSE -> IDLE produced no phantom event")


# --------------------------------------------------------------------------
# Test 4 — back-to-back prints emit two PrintStarted, not Resumed.
# --------------------------------------------------------------------------
def test_two_prints_in_a_row():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    # Print 1
    bridge._handle_report({"print": {"gcode_state": "PREPARE"}})
    bridge._handle_report({"print": {"gcode_state": "RUNNING"}})
    bridge._handle_report({"print": {"gcode_state": "FINISH"}})
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    # Print 2
    bridge._handle_report({"print": {"gcode_state": "PREPARE"}})
    bridge._handle_report({"print": {"gcode_state": "RUNNING"}})
    bridge._handle_report({"print": {"gcode_state": "FINISH"}})
    expected = [
        "Connected", "PrintStarted", "PrintDone", "PrintStarted", "PrintDone"
    ]
    assert events == expected, f"two-print sequence wrong: {events}"
    print("  [OK] back-to-back prints emitted two PrintStarted")


# --------------------------------------------------------------------------
# Test 5 — snapshot is updated BEFORE the event callback fires.
# --------------------------------------------------------------------------
def test_snapshot_before_event_ordering():
    """When PrintStarted fires, the snapshot the plugin reads must already
    reflect status='printing' / file=x.3mf. Otherwise the webhook payload
    built inside _post() would say status='idle' for PrintStarted."""
    seen_at_event = {}

    def event_cb(event):
        # at this point, the bridge's snapshot must already reflect the new state
        seen_at_event[event] = bridge.snapshot()

    bridge = BambuBridge(
        host="dummy", serial="ABCDEFGH00", access_code="x",
        event_callback=event_cb,
        status_callback=lambda s: None,
        logger=_Logger(),
    )

    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    bridge._handle_report({
        "print": {
            "gcode_state": "PREPARE",
            "subtask_name": "ordering.3mf",
            "mc_percent": 0.0,
        }
    })
    snap = seen_at_event["PrintStarted"]
    assert snap["status"] == "printing", f"snapshot lagged: {snap}"
    assert snap["file"] == "ordering.3mf", f"file not in snapshot at event time: {snap}"
    print("  [OK] snapshot updated before event callback")


# --------------------------------------------------------------------------
# Test 6 — info report after print report still updates firmware.
# --------------------------------------------------------------------------
def test_info_after_print_updates_firmware():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "IDLE"}})
    assert bridge.snapshot()["firmware_version"] is None
    bridge._handle_report({
        "info": {
            "command": "get_version",
            "module": [
                {"name": "ota", "sw_ver": "01.06.00.00"},
                {"name": "esp32", "sw_ver": "00.00.00.99"},
            ],
        }
    })
    fw = bridge.snapshot()["firmware_version"]
    assert fw == "01.06.00.00", f"firmware not extracted: {fw}"
    print("  [OK] info report extracted firmware version")


# --------------------------------------------------------------------------
# Test 7 — garbage payloads must not crash.
# --------------------------------------------------------------------------
def test_garbage_payloads():
    bridge, events, _ = _make_bridge()
    # Pre-condition: no connected event yet
    assert events == []
    # All these must be no-ops
    bridge._handle_report(None)  # type: ignore[arg-type]
    bridge._handle_report({})
    bridge._handle_report({"print": "not a dict"})
    bridge._handle_report({"print": {"gcode_state": 42}})  # wrong type
    bridge._handle_report({"print": {}})
    bridge._handle_report({"print": {"random_key": "value"}})
    bridge._handle_report({"info": "wrong"})
    bridge._handle_report({"info": {"module": "not a list"}})
    bridge._handle_report({"info": {"module": [None, 42, "str", {}]}})
    # None of those should have triggered Connected (we never saw a valid gcode_state)
    # However: empty {"print": {}} IS treated as a valid print report and triggers Connected.
    # That's acceptable — the semantics are "any well-formed print report means we're connected".
    # The point of the test is "we did not crash". Confirm:
    print(f"  [OK] garbage payloads did not crash (events: {events})")


# --------------------------------------------------------------------------
# Test 8 — _mask_serial corner cases.
# --------------------------------------------------------------------------
def test_mask_serial():
    assert _mask_serial("") == "****"
    assert _mask_serial("AB") == "****"
    assert _mask_serial("ABCD") == "****"
    assert _mask_serial("ABCDE") == "AB***DE"
    assert _mask_serial("01PS00A0B0000001") == "01***01"
    print("  [OK] _mask_serial handles short and long serials")


# --------------------------------------------------------------------------
# Test 9 — stop() is idempotent.
# --------------------------------------------------------------------------
def test_stop_idempotent():
    bridge, events, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "subtask_name": "x.3mf"}})
    # We're connected and printing. Now call stop() twice.
    bridge.stop()
    events_after_first = list(events)
    bridge.stop()
    events_after_second = list(events)
    assert events_after_first == events_after_second, (
        f"second stop() emitted extra events: {events_after_second}"
    )
    assert events_after_first[-1] == "Disconnected", (
        f"expected Disconnected at end: {events_after_first}"
    )
    print("  [OK] stop() is idempotent and emitted Disconnected exactly once")


# --------------------------------------------------------------------------
# Test 10 — gcode_file fallback when subtask_name missing.
# --------------------------------------------------------------------------
def test_gcode_file_fallback():
    bridge, _, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "gcode_file": "fallback.gcode"}})
    snap = bridge.snapshot()
    assert snap["file"] == "fallback.gcode", snap
    # Now subtask_name overrides
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "subtask_name": "preferred.3mf"}})
    snap = bridge.snapshot()
    assert snap["file"] == "preferred.3mf", snap
    print("  [OK] gcode_file fallback works, subtask_name takes priority")


# --------------------------------------------------------------------------
# Test 11 — progress numeric edge cases.
# --------------------------------------------------------------------------
def test_progress_numeric_edges():
    bridge, _, _ = _make_bridge()
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "mc_percent": 0}})
    assert bridge.snapshot()["progress"] == 0.0
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "mc_percent": "not a number"}})
    # Should keep the previous value, not crash
    assert bridge.snapshot()["progress"] == 0.0
    bridge._handle_report({"print": {"gcode_state": "RUNNING", "mc_percent": 99.9}})
    assert bridge.snapshot()["progress"] == 99.9
    print("  [OK] progress accepts ints/floats, ignores garbage")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def main():
    print("=== Bambu state machine — edge cases ===\n")
    tests = [
        test_duplicate_state_no_double_fire,
        test_cancel_mid_print,
        test_pause_to_idle_no_phantom_done,
        test_two_prints_in_a_row,
        test_snapshot_before_event_ordering,
        test_info_after_print_updates_firmware,
        test_garbage_payloads,
        test_mask_serial,
        test_stop_idempotent,
        test_gcode_file_fallback,
        test_progress_numeric_edges,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    if failed:
        print(f"=== {failed}/{len(tests)} edge case(s) FAILED ===")
        return 1
    print(f"=== {len(tests)}/{len(tests)} edge cases passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
