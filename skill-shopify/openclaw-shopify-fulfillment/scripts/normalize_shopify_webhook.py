#!/usr/bin/env python3
"""
Normalize Shopify webhook payloads into the Openclaw mirror envelope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SUPPORTED_TOPICS = {"orders/paid", "fulfillments/create"}


def is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def first_non_blank(*values: Any) -> Any:
    for value in values:
        if not is_blank(value):
            return value
    return None


def pick(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and not is_blank(mapping[key]):
            return mapping[key]
    return None


def as_list(value: Any) -> list[Any]:
    if is_blank(value):
        return []
    if isinstance(value, list):
        return value
    return [value]


def push_unique(items: list[Any], value: Any) -> None:
    if not is_blank(value) and value not in items:
        items.append(value)


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            compacted = compact(item)
            if not is_blank(compacted):
                result[key] = compacted
        return result
    if isinstance(value, list):
        result = [compact(item) for item in value]
        return [item for item in result if not is_blank(item)]
    return value


def join_name(*parts: Any) -> str | None:
    cleaned = [str(part).strip() for part in parts if not is_blank(part)]
    if not cleaned:
        return None
    return " ".join(cleaned)


def build_key(*parts: Any) -> str | None:
    cleaned = [str(part).strip() for part in parts if not is_blank(part)]
    if not cleaned:
        return None
    return ":".join(cleaned)


def normalize_address(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return compact(
        {
            "name": first_non_blank(
                pick(raw, "name"),
                join_name(
                    pick(raw, "first_name", "firstName"),
                    pick(raw, "last_name", "lastName"),
                ),
            ),
            "first_name": pick(raw, "first_name", "firstName"),
            "last_name": pick(raw, "last_name", "lastName"),
            "company": pick(raw, "company"),
            "address1": pick(raw, "address1"),
            "address2": pick(raw, "address2"),
            "city": pick(raw, "city"),
            "province": pick(raw, "province"),
            "province_code": pick(raw, "province_code", "provinceCode"),
            "country": pick(raw, "country"),
            "country_code": pick(raw, "country_code", "countryCode", "countryCodeV2"),
            "zip": pick(raw, "zip"),
            "phone": pick(raw, "phone"),
        }
    )


def normalize_line_items(raw_items: Any) -> list[dict[str, Any]]:
    items = []
    for raw in as_list(raw_items):
        line_item = pick(raw, "lineItem")
        variant = raw.get("variant") if isinstance(raw, dict) else None
        if not isinstance(line_item, dict):
            line_item = None
        if not isinstance(variant, dict):
            variant = None
        items.append(
            compact(
                {
                    "id": first_non_blank(pick(raw, "id"), pick(line_item, "id")),
                    "variant_id": first_non_blank(
                        pick(raw, "variant_id", "variantId"),
                        pick(variant, "id"),
                    ),
                    "sku": first_non_blank(pick(raw, "sku"), pick(line_item, "sku")),
                    "title": first_non_blank(
                        pick(raw, "title", "name"),
                        pick(line_item, "title", "name"),
                    ),
                    "quantity": first_non_blank(
                        pick(raw, "quantity"),
                        pick(raw, "totalQuantity"),
                        pick(raw, "remainingQuantity"),
                    ),
                }
            )
        )
    return [item for item in items if item]


def extract_tracking(*sources: Any) -> dict[str, Any]:
    tracking_numbers: list[Any] = []
    tracking_urls: list[Any] = []
    tracking_company = None
    label_url = None

    for source in sources:
        if not isinstance(source, dict):
            continue
        tracking_company = first_non_blank(
            tracking_company,
            pick(source, "tracking_company", "trackingCompany"),
        )
        for value in as_list(pick(source, "tracking_numbers", "trackingNumbers")):
            push_unique(tracking_numbers, value)
        push_unique(tracking_numbers, pick(source, "tracking_number", "trackingNumber"))
        for value in as_list(pick(source, "tracking_urls", "trackingUrls")):
            push_unique(tracking_urls, value)
        push_unique(tracking_urls, pick(source, "tracking_url", "trackingUrl"))
        receipt = source.get("receipt") if isinstance(source.get("receipt"), dict) else {}
        label_url = first_non_blank(
            label_url,
            pick(source, "label_url", "labelUrl"),
            pick(receipt, "label_url", "labelUrl"),
        )

    return compact(
        {
            "carrier": tracking_company,
            "tracking_numbers": tracking_numbers,
            "tracking_urls": tracking_urls,
            "label_url": label_url,
        }
    )


def normalize_orders_paid(
    payload: dict[str, Any],
    shop_domain: str,
    webhook_id: str | None,
    event_id: str | None,
    triggered_at: str | None,
    payload_path: str,
) -> dict[str, Any]:
    order_id = pick(payload, "id", "order_id")
    fulfillments = as_list(payload.get("fulfillments"))
    tracking = extract_tracking(payload, *fulfillments)
    shipping_address = normalize_address(pick(payload, "shipping_address", "shippingAddress"))
    line_items = normalize_line_items(pick(payload, "line_items", "lineItems"))

    missing_fields = []
    if is_blank(order_id):
        missing_fields.append("order.id")
    if is_blank(shipping_address):
        missing_fields.append("order.shipping_address")
    if not line_items:
        missing_fields.append("order.line_items")

    order = compact(
        {
            "id": str(order_id) if not is_blank(order_id) else None,
            "gid": pick(payload, "admin_graphql_api_id", "adminGraphqlApiId"),
            "reference": pick(payload, "name"),
            "email": first_non_blank(pick(payload, "contact_email", "contactEmail"), pick(payload, "email")),
            "financial_status": pick(payload, "financial_status", "displayFinancialStatus"),
            "fulfillment_status": first_non_blank(
                pick(payload, "fulfillment_status"),
                pick(payload, "display_fulfillment_status", "displayFulfillmentStatus"),
            ),
            "currency": pick(payload, "currency"),
            "shipping_address": shipping_address,
            "items": line_items,
        }
    )

    fulfillment = compact(
        {
            "id": None,
            "status": None,
            **tracking,
        }
    )

    record_key = build_key(shop_domain, order.get("id"), "order")
    event_key = build_key(shop_domain, event_id or webhook_id or order.get("id") or "orders-paid")
    delivery_key = build_key(shop_domain, webhook_id or event_id or order.get("id") or "orders-paid")

    return compact(
        {
            "source": "shopify",
            "strategy": "mirror",
            "topic": "orders/paid",
            "shop_domain": shop_domain,
            "event_key": event_key,
            "delivery_key": delivery_key,
            "triggered_at": triggered_at,
            "order": order,
            "fulfillment": fulfillment,
            "openclaw": {
                "record_key": record_key,
                "operator_action": "print_label" if fulfillment.get("label_url") else "prepare_shipment",
                "ready_to_ship": bool(fulfillment.get("label_url") or fulfillment.get("tracking_urls")),
                "needs_api_enrichment": True,
                "missing_fields": missing_fields,
            },
            "metadata": {
                "payload_path": payload_path,
            },
        }
    )


def normalize_fulfillments_create(
    payload: dict[str, Any],
    shop_domain: str,
    webhook_id: str | None,
    event_id: str | None,
    triggered_at: str | None,
    payload_path: str,
) -> dict[str, Any]:
    order_id = pick(payload, "order_id", "orderId")
    fulfillment_id = pick(payload, "id", "fulfillment_id", "fulfillmentId")
    destination = normalize_address(pick(payload, "destination", "shipping_address", "shippingAddress"))
    line_items = normalize_line_items(pick(payload, "line_items", "lineItems"))
    tracking = extract_tracking(payload)

    missing_fields = []
    if is_blank(order_id):
        missing_fields.append("order.id")
    if is_blank(fulfillment_id):
        missing_fields.append("fulfillment.id")
    if is_blank(destination):
        missing_fields.append("fulfillment.destination")
    if not tracking.get("tracking_numbers") and not tracking.get("tracking_urls"):
        missing_fields.append("fulfillment.tracking")

    order = compact(
        {
            "id": str(order_id) if not is_blank(order_id) else None,
            "reference": pick(payload, "name"),
            "email": pick(payload, "email"),
            "shipping_address": destination,
            "items": line_items,
        }
    )

    fulfillment = compact(
        {
            "id": str(fulfillment_id) if not is_blank(fulfillment_id) else None,
            "gid": pick(payload, "admin_graphql_api_id", "adminGraphqlApiId"),
            "status": pick(payload, "status"),
            **tracking,
        }
    )

    record_key = build_key(shop_domain, order.get("id"), fulfillment.get("id") or "fulfillment")
    event_key = build_key(
        shop_domain,
        event_id or webhook_id or fulfillment.get("id") or order.get("id") or "fulfillments-create",
    )
    delivery_key = build_key(
        shop_domain,
        webhook_id or event_id or fulfillment.get("id") or order.get("id") or "fulfillments-create",
    )

    return compact(
        {
            "source": "shopify",
            "strategy": "mirror",
            "topic": "fulfillments/create",
            "shop_domain": shop_domain,
            "event_key": event_key,
            "delivery_key": delivery_key,
            "triggered_at": triggered_at,
            "order": order,
            "fulfillment": fulfillment,
            "openclaw": {
                "record_key": record_key,
                "operator_action": "print_label" if fulfillment.get("label_url") else "track_shipment",
                "ready_to_ship": True,
                "needs_api_enrichment": bool(missing_fields),
                "missing_fields": missing_fields,
            },
            "metadata": {
                "payload_path": payload_path,
            },
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize a Shopify webhook payload into the Openclaw mirror envelope.",
    )
    parser.add_argument("--topic", required=True, choices=sorted(SUPPORTED_TOPICS))
    parser.add_argument("--payload", required=True, help="Path to a JSON payload file.")
    parser.add_argument(
        "--shop-domain",
        default="unknown.myshopify.com",
        help="Shopify shop domain.",
    )
    parser.add_argument("--webhook-id", help="Value of X-Shopify-Webhook-Id.")
    parser.add_argument("--event-id", help="Value of X-Shopify-Event-Id.")
    parser.add_argument("--triggered-at", help="Value of X-Shopify-Triggered-At.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload_path = Path(args.payload).resolve()
    payload = json.loads(payload_path.read_text())

    if args.topic == "orders/paid":
        normalized = normalize_orders_paid(
            payload=payload,
            shop_domain=args.shop_domain,
            webhook_id=args.webhook_id,
            event_id=args.event_id,
            triggered_at=args.triggered_at,
            payload_path=str(payload_path),
        )
    else:
        normalized = normalize_fulfillments_create(
            payload=payload,
            shop_domain=args.shop_domain,
            webhook_id=args.webhook_id,
            event_id=args.event_id,
            triggered_at=args.triggered_at,
            payload_path=str(payload_path),
        )

    print(json.dumps(normalized, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
