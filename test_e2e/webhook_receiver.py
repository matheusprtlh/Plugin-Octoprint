"""Servidor HTTP simples que registra POSTs de webhook.

Mimetiza o site do cliente recebendo eventos do plugin. Loga timestamp,
header Authorization (mascarado), e o body JSON pretty-printed em
``test_e2e/received.log`` e no stdout.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_PATH = Path(__file__).parent / "received.log"


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — http.server convention
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}

        auth = self.headers.get("Authorization", "")
        masked_auth = (
            auth[:13] + "..." + auth[-4:] if len(auth) > 20 else auth
        )

        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "path": self.path,
            "auth_header": masked_auth or "(missing)",
            "payload": payload,
        }

        # JSONL: um record por linha, fácil de parsear depois.
        line = json.dumps(record, ensure_ascii=False)
        pretty = json.dumps(record, ensure_ascii=False, indent=2)
        print("---")
        print(pretty)
        print("---", flush=True)

        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):  # silencia o log padrão do http.server
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="webhook_receiver")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--reset-log", action="store_true",
        help="Apaga received.log antes de iniciar",
    )
    args = parser.parse_args(argv)

    if args.reset_log and LOG_PATH.exists():
        LOG_PATH.unlink()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), WebhookHandler)
    print(f"webhook receiver ouvindo em http://127.0.0.1:{args.port}", flush=True)
    print(f"log: {LOG_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main() or 0)
