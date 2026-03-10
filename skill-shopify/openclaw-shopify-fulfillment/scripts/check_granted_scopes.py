#!/usr/bin/env python3
"""
Compare granted Shopify scopes against task-oriented capability bundles.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


CAPABILITY_SCOPES = {
    "fulfillment-mirror": {
        "read_orders",
        "read_fulfillments",
    },
    "commerce-ops": {
        "read_orders",
        "write_orders",
        "read_fulfillments",
        "write_fulfillments",
        "read_inventory",
        "write_inventory",
        "read_locations",
        "write_locations",
        "read_returns",
        "write_returns",
        "read_shipping",
        "write_shipping",
    },
    "catalog-ops": {
        "read_products",
        "write_products",
        "read_inventory",
        "write_inventory",
        "read_files",
        "write_files",
        "read_metaobjects",
        "write_metaobjects",
        "read_metaobject_definitions",
        "write_metaobject_definitions",
    },
    "seo-content": {
        "read_content",
        "write_content",
        "read_online_store_navigation",
        "write_online_store_navigation",
        "read_metaobjects",
        "write_metaobjects",
        "read_metaobject_definitions",
        "write_metaobject_definitions",
        "read_files",
        "write_files",
        "read_translations",
        "write_translations",
        "read_locales",
        "write_locales",
    },
    "marketing-analytics": {
        "read_reports",
        "read_marketing_events",
        "write_marketing_events",
        "read_discounts",
        "write_discounts",
        "read_price_rules",
        "write_price_rules",
        "write_pixels",
        "read_customer_events",
    },
    "design-storefront": {
        "read_themes",
        "write_themes",
        "read_files",
        "write_files",
        "read_content",
        "write_content",
        "read_checkout_branding_settings",
        "write_checkout_branding_settings",
    },
    "customer-crm": {
        "read_customers",
        "write_customers",
        "read_customer_name",
        "read_customer_email",
        "read_customer_phone",
        "read_customer_address",
    },
    "markets-i18n": {
        "read_markets",
        "write_markets",
        "read_translations",
        "write_translations",
        "read_locales",
        "write_locales",
    },
}


def parse_scopes(raw: str) -> set[str]:
    return {scope.strip() for scope in raw.split(",") if scope.strip()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Shopify granted scopes against capability bundles.",
    )
    parser.add_argument(
        "--scopes",
        required=True,
        help="Comma-separated granted scopes string.",
    )
    parser.add_argument(
        "--capability",
        choices=sorted(CAPABILITY_SCOPES),
        help="Check one capability only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a text report.",
    )
    return parser


def evaluate(granted: set[str], capability: str) -> dict[str, object]:
    required = CAPABILITY_SCOPES[capability]
    missing = sorted(required - granted)
    extra = sorted(granted - required)
    return {
        "capability": capability,
        "required": sorted(required),
        "granted": sorted(granted),
        "missing": missing,
        "extra": extra,
        "ready": not missing,
    }


def main() -> int:
    args = build_parser().parse_args()
    granted = parse_scopes(args.scopes)
    capabilities = [args.capability] if args.capability else sorted(CAPABILITY_SCOPES)
    results = [evaluate(granted, capability) for capability in capabilities]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    for result in results:
        print(f"[{result['capability']}] ready={str(result['ready']).lower()}")
        if result["missing"]:
            print("  missing: " + ", ".join(result["missing"]))
        else:
            print("  missing: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
