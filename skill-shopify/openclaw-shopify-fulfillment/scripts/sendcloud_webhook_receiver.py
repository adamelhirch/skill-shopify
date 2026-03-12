#!/usr/bin/env python3
"""
Minimal Sendcloud webhook receiver.

Features:
- Verify Sendcloud webhook signature (HMAC-SHA256 hex)
- Persist inbound events in JSONL
- Simple health endpoint
"""

from __future__ import annotations

import argparse
import hmac
import hashlib
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def get_signature_header(headers) -> str | None:
    for key in ("Sendcloud-Signature", "X-Sendcloud-Signature", "sendcloud-signature"):
        value = headers.get(key)
        if value:
            return value.strip()
    return None


def verify_signature(secret: str | None, body: bytes, provided_signature: str | None) -> tuple[bool, str | None]:
    if not secret:
        return True, None
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not provided_signature:
        return False, expected
    return hmac.compare_digest(expected, provided_signature), expected


def make_handler(webhook_path: str, secret: str | None, events_log_path: Path, strict_signature: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SendcloudWebhookReceiver/1.0"

        def log_message(self, format: str, *args) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/healthz":
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "sendcloud-webhook-receiver",
                        "webhook_path": webhook_path,
                        "timestamp": utc_now(),
                    },
                )
                return
            json_response(self, 404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            if self.path != webhook_path:
                json_response(self, 404, {"ok": False, "error": "Invalid webhook path"})
                return

            length_raw = self.headers.get("Content-Length", "0")
            try:
                length = int(length_raw)
            except ValueError:
                length = 0
            body = self.rfile.read(max(0, length))

            provided_sig = get_signature_header(self.headers)
            signature_valid, expected_sig = verify_signature(secret, body, provided_sig)

            record: dict[str, Any] = {
                "received_at": utc_now(),
                "path": self.path,
                "method": "POST",
                "headers": {key: value for key, value in self.headers.items()},
                "signature": {
                    "provided": provided_sig,
                    "expected": expected_sig,
                    "valid": signature_valid,
                    "strict_signature": strict_signature,
                },
                "raw_body": body.decode("utf-8", errors="replace"),
            }

            try:
                record["json_body"] = json.loads(record["raw_body"])
            except json.JSONDecodeError:
                record["json_body"] = None
                record["json_error"] = "Invalid JSON payload"

            events_log_path.parent.mkdir(parents=True, exist_ok=True)
            with events_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")

            if strict_signature and not signature_valid:
                json_response(self, 401, {"ok": False, "error": "Invalid Sendcloud signature"})
                return

            json_response(self, 200, {"ok": True})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a minimal Sendcloud webhook receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--webhook-path", default="/webhooks/sendcloud")
    parser.add_argument("--signature-key", default=None)
    parser.add_argument("--events-log", default="tmp/sendcloud-webhook-events.jsonl")
    parser.add_argument("--no-strict-signature", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    strict_signature = not args.no_strict_signature
    handler = make_handler(
        webhook_path=args.webhook_path,
        secret=args.signature_key,
        events_log_path=Path(args.events_log).resolve(),
        strict_signature=strict_signature,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "sendcloud-webhook-receiver",
                "listen": f"http://{args.host}:{args.port}",
                "webhook_path": args.webhook_path,
                "strict_signature": strict_signature,
                "events_log": str(Path(args.events_log).resolve()),
            },
            ensure_ascii=True,
        )
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
