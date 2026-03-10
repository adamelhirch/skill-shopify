#!/usr/bin/env python3
"""
Compute or verify Shopify webhook HMAC values from a raw payload file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path


def load_secret(secret: str | None, secret_env: str | None) -> str:
    if secret:
        return secret
    if secret_env:
        value = os.getenv(secret_env)
        if value:
            return value
        raise SystemExit(f"Environment variable not set or empty: {secret_env}")
    raise SystemExit("Provide --secret or --secret-env.")


def compute_hmac(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute or verify a Shopify X-Shopify-Hmac-Sha256 value.",
    )
    parser.add_argument("--payload", required=True, help="Path to the raw payload file.")
    parser.add_argument("--secret", help="Webhook secret.")
    parser.add_argument(
        "--secret-env",
        help="Environment variable that contains the webhook secret.",
    )
    parser.add_argument(
        "--header",
        help="Received X-Shopify-Hmac-Sha256 value to verify.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload_path = Path(args.payload).resolve()
    payload = payload_path.read_bytes()
    secret = load_secret(args.secret, args.secret_env)
    expected_hmac = compute_hmac(secret, payload)

    if args.header:
        result = {
            "payload": str(payload_path),
            "expected_hmac": expected_hmac,
            "provided_hmac": args.header.strip(),
        }
        result["valid"] = hmac.compare_digest(
            result["expected_hmac"],
            result["provided_hmac"],
        )
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 1

    if args.json:
        print(
            json.dumps(
                {
                    "payload": str(payload_path),
                    "expected_hmac": expected_hmac,
                },
                indent=2,
            )
        )
        return 0

    print(expected_hmac)
    return 0


if __name__ == "__main__":
    sys.exit(main())
