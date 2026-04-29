"""End-to-end exerciser do plugin sem rodar o OctoPrint completo.

Importa ``MarketplaceLinkPlugin`` (com stubs de ``octoprint.plugin`` e
``octoprint.util`` em ``test_e2e/octoprint/``), injeta os atributos que o
OctoPrint normalmente preencheria (``_settings``, ``_printer``, ``_logger``),
configura o webhook URL para o receiver local e dispara a sequência de
eventos que uma impressão real produziria. Verifica que o receiver coletou
os payloads esperados.

Uso (com o ``webhook_receiver.py`` rodando em paralelo):

    python test_e2e/run_test.py
"""

import json
import sys
import time
from pathlib import Path

# Adiciona os stubs de octoprint primeiro. Por padrão usa o source de
# plugin/; se PLUGIN_USE_INSTALLED=1 estiver setado, importa do site-packages
# (testa o wheel/sdist gerado em vez do source bruto).
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))                                  # stubs
import os as _os
if _os.environ.get("PLUGIN_USE_INSTALLED") != "1":
    sys.path.insert(0, str(HERE.parent / "plugin"))            # source
RECEIVED_LOG = HERE / "received.log"


# ============================================================
# Mocks dos atributos que o OctoPrint injeta na instância
# ============================================================


class _Settings:
    """Mock do ``self._settings`` do OctoPrint.

    Os métodos espelham a API que o plugin usa: ``get``, ``get_int``,
    ``set``, ``set_int``, ``save``, ``global_get``.
    """

    def __init__(self, defaults: dict) -> None:
        self._values = dict(defaults)

    def get(self, path):
        return self._values.get(path[0])

    def get_int(self, path):
        v = self._values.get(path[0])
        try:
            return int(v) if v not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    def set(self, path, value):
        self._values[path[0]] = value

    def set_int(self, path, value):
        self._values[path[0]] = int(value or 0)

    def save(self):
        return None

    def global_get(self, path):
        if path == ["appearance", "name"]:
            return "VirtualPrinter (e2e)"
        return None


class _Printer:
    """Mock do ``self._printer`` do OctoPrint, controlável pelo harness."""

    def __init__(self):
        self._state = "operational"  # operational | printing | error | offline
        self.firmware_info = {"name": "Marlin 2.1.x (virtual)"}
        self._job = {"file": None, "completion": None}

    # ---- API que o plugin consome ----
    def is_printing(self):
        return self._state == "printing"

    def is_error(self):
        return self._state == "error"

    def is_operational(self):
        return self._state in {"operational", "printing"}

    def get_current_data(self):
        return {
            "job": {"file": {"name": self._job["file"]}},
            "progress": {"completion": self._job["completion"]},
        }

    # ---- helpers do harness ----
    def go_offline(self):
        self._state = "offline"

    def go_idle(self):
        self._state = "operational"
        self._job = {"file": None, "completion": None}

    def start_printing(self, filename):
        self._state = "printing"
        self._job = {"file": filename, "completion": 0.0}

    def progress(self, pct):
        if self._state == "printing":
            self._job["completion"] = pct

    def go_error(self):
        self._state = "error"


class _Logger:
    def debug(self, msg, *args):
        print("[plugin DEBUG]", msg % args if args else msg)

    def info(self, msg, *args):
        print("[plugin INFO ]", msg % args if args else msg)

    def warning(self, msg, *args):
        print("[plugin WARN ]", msg % args if args else msg)

    def exception(self, msg, *args):
        print("[plugin EXC  ]", msg % args if args else msg)


# ============================================================
# Bootstrap
# ============================================================

def _instantiate_plugin(webhook_url, token, brand, model, heartbeat_minutes):
    from octoprint_marketplace_link import MarketplaceLinkPlugin  # type: ignore

    plugin = MarketplaceLinkPlugin()
    defaults = plugin.get_settings_defaults()
    defaults.update(
        {
            "webhook_url": webhook_url,
            "auth_token": token,
            "brand": brand,
            "model": model,
            "heartbeat_minutes": heartbeat_minutes,
        }
    )
    plugin._settings = _Settings(defaults)
    plugin._printer = _Printer()
    plugin._logger = _Logger()
    plugin._plugin_version = "0.3.0"
    return plugin


def _read_log():
    if not RECEIVED_LOG.exists():
        return []
    records = []
    for line in RECEIVED_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _wait_for_deliveries(target_count, timeout_s=15):
    """Aguarda até ``target_count`` records no received.log ou timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        records = _read_log()
        if len(records) >= target_count:
            return records
        time.sleep(0.1)
    return _read_log()


def main():
    if RECEIVED_LOG.exists():
        RECEIVED_LOG.unlink()

    print("=== E2E test do plugin OctoPrint-MarketplaceLink ===\n")

    plugin = _instantiate_plugin(
        webhook_url="http://127.0.0.1:9000/incoming",
        token="test-token-abc123",
        brand="creality",
        model="Ender 3 V2",
        heartbeat_minutes=0,  # desativa heartbeat periódico no teste de eventos
    )

    print("[harness] startup")
    plugin.on_after_startup()

    # ---- Sequência típica de uma impressão ----
    sequence = [
        ("Connected",       lambda: plugin._printer.go_idle()),
        ("PrintStarted",    lambda: plugin._printer.start_printing("benchy.gcode")),
        ("PrintPaused",     lambda: None),  # pausa no meio
        ("PrintResumed",    lambda: plugin._printer.progress(50.0)),
        ("PrintDone",       lambda: (plugin._printer.progress(100.0), plugin._printer.go_idle())),
        ("Disconnected",    lambda: plugin._printer.go_offline()),
    ]

    expected = len(sequence)
    print(f"[harness] disparando {expected} eventos...\n")
    for event, mutate in sequence:
        result = mutate() if callable(mutate) else None
        print(f"[harness] -> on_event({event!r})")
        plugin.on_event(event, payload={})
        time.sleep(0.3)  # dá tempo do thread daemon postar

    # Confere quantos chegaram
    print(f"\n[harness] aguardando até {expected} entregas no receiver...")
    records = _wait_for_deliveries(expected, timeout_s=10)
    print(f"[harness] {len(records)}/{expected} entregas registradas em received.log")

    # ---- Teste do botão "Testar webhook" ----
    print("\n[harness] simulando clique em 'Testar webhook'...")
    ok, status, error = plugin._post(event="test", record_failure=False)
    print(f"[harness] test_webhook: ok={ok} status={status} error={error!r}")

    # ---- Teste do heartbeat periódico ----
    print("\n[harness] ativando heartbeat de 1s e esperando 2 ticks...")
    plugin._settings.set_int(["heartbeat_minutes"], 0)  # zera primeiro
    plugin._stop_heartbeat()
    # forçar intervalo de 1s sobrescrevendo o método
    plugin._heartbeat_seconds = lambda: 1
    plugin._start_heartbeat()
    time.sleep(2.5)
    plugin._stop_heartbeat()
    print("[harness] heartbeat parado")

    # ---- Teste de erro: webhook URL inválida ----
    print("\n[harness] simulando webhook caído (URL errada)...")
    plugin._settings.set(["webhook_url"], "http://127.0.0.1:9999/dead")
    plugin.on_event("Error", payload={})
    # daemon thread precisa terminar o post + record_delivery; ConnectionRefused
    # é rápido mas o lock pode estar contended.
    time.sleep(3.0)
    print(
        f"[harness] last_delivery_status={plugin._settings.get_int(['last_delivery_status'])} "
        f"last_delivery_error={plugin._settings.get(['last_delivery_error'])!r}"
    )

    # ---- Resumo da telemetria ----
    print("\n=== Telemetria final no plugin ===")
    for key in (
        "webhook_url",
        "brand",
        "model",
        "last_delivery_at",
        "last_delivery_status",
        "last_delivery_error",
    ):
        print(f"  {key}: {plugin._settings.get([key])!r}")

    # ---- Resumo do que chegou no receiver ----
    print("\n=== Resumo do received.log ===")
    records = _read_log()
    if not records:
        print("  (vazio — nenhuma entrega registrada)")
        return 1

    print(f"  Total de POSTs: {len(records)}")
    events = [(r.get("payload") or {}).get("event") for r in records]
    print(f"  Eventos em ordem: {events}")
    print(f"  Primeiro auth_header: {records[0].get('auth_header')!r}")
    print("\n  Exemplo de payload (primeiro PrintStarted):")
    for r in records:
        if (r.get("payload") or {}).get("event") == "PrintStarted":
            print(json.dumps(r["payload"], ensure_ascii=False, indent=2))
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
