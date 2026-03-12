import argparse
import json
from pathlib import Path

from carrier_rate_clients import quote_service_rate
from shopify_admin_ops import fail, graph_ql, output, resolve_context, resolve_order_id


DEFAULT_RATES_POLICY_FILE = Path(__file__).resolve().parents[1] / "assets" / "manual-rate-policy.json"
EU_COUNTRY_CODES = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
}
TEST_PACKAGES = [
    {
        "code": "sendcloud_default_20x15x5",
        "label": "Sendcloud default parcel",
        "inner_length_cm": 20.0,
        "inner_width_cm": 15.0,
        "inner_height_cm": 5.0,
        "empty_weight_kg": 0.02,
        "max_weight_kg": 1.0,
        "enabled": True,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a shipment plan from Shopify order data and a package catalog")
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    parser.add_argument("--order-id")
    parser.add_argument("--order-name")
    parser.add_argument("--packages-file")
    parser.add_argument("--rates-policy-file", default=str(DEFAULT_RATES_POLICY_FILE))
    parser.add_argument("--rate-source", choices=["policy", "live", "auto"], default="auto")
    parser.add_argument("--strict-live-rates", action="store_true")
    parser.add_argument("--carrier-timeout-sec", type=int, default=20)
    parser.add_argument("--no-rate-estimate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_packages(path: str | None) -> tuple[list[dict], str]:
    if not path:
        return list(TEST_PACKAGES), "built-in test package constants"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        fail(f"Could not read packages file: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid packages JSON: {exc}")
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        fail("packages-file must contain a non-empty packages array")
    valid = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        if package.get("enabled", True) is False:
            continue
        valid.append(package)
    if not valid:
        fail("No enabled packages found in packages-file")
    return valid, str(Path(path).resolve())


def load_rates_policy(path: str | None) -> tuple[dict | None, str | None]:
    if not path:
        return None, None
    policy_path = Path(path)
    if not policy_path.exists():
        return None, None
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"Could not read rates policy file: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid rates policy JSON: {exc}")

    if not isinstance(payload, dict):
        fail("rates policy file must contain a JSON object")

    zones = payload.get("zones")
    if not isinstance(zones, list) or not zones:
        return None, str(policy_path.resolve())

    payload["currency_code"] = str(payload.get("currency_code") or "EUR")
    payload["default_margin_percent"] = float(payload.get("default_margin_percent", 0.0))
    payload["default_margin_fixed"] = float(payload.get("default_margin_fixed", 0.0))
    payload["default_min_price"] = float(payload.get("default_min_price", 0.0))
    payload["default_max_price"] = float(payload.get("default_max_price", 999999.0))
    payload["active_colis_type"] = str(payload.get("active_colis_type", "")).strip() or None
    payload["colis_types"] = payload.get("colis_types") or {}
    payload["zones"] = zones
    return payload, str(policy_path.resolve())


def get_order(context: dict, order_gid: str) -> dict:
    data = graph_ql(
        context,
        """
        query ShipmentPlanOrder($id: ID!) {
          order(id: $id) {
            id
            name
            email
            shippingAddress {
              firstName
              lastName
              company
              address1
              address2
              city
              province
              zip
              country
              countryCodeV2
              phone
            }
            lineItems(first: 100) {
              nodes {
                id
                name
                quantity
                sku
                variant {
                  id
                }
              }
            }
          }
        }
        """,
        {"id": order_gid},
    )
    order = data.get("order")
    if not order:
        fail("Order not found")
    return order


def get_variants(context: dict, variant_ids: list[str]) -> dict[str, dict]:
    nodes = graph_ql(
        context,
        """
        query ShipmentPlanVariants($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on ProductVariant {
              id
              title
              sku
              inventoryItem {
                id
                measurement {
                  weight {
                    value
                    unit
                  }
                }
              }
              product {
                id
                title
                handle
              }
              metafields(first: 20, namespace: "openclaw_logistics") {
                nodes {
                  key
                  value
                  type
                }
              }
            }
          }
        }
        """,
        {"ids": variant_ids},
    ).get("nodes") or []
    by_id = {}
    for node in nodes:
        if node:
            by_id[node["id"]] = node
    return by_id


def decimal_metafield(variant: dict, key: str) -> float | None:
    for metafield in variant.get("metafields", {}).get("nodes", []):
        if metafield.get("key") == key:
            try:
                return float(metafield.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def text_metafield(variant: dict, key: str) -> str | None:
    for metafield in variant.get("metafields", {}).get("nodes", []):
        if metafield.get("key") == key:
            value = metafield.get("value")
            return str(value) if value is not None else None
    return None


def normalized_dims(length_cm: float, width_cm: float, height_cm: float) -> list[float]:
    return sorted([length_cm, width_cm, height_cm], reverse=True)


def compute_price(cost: float, margin_percent: float, margin_fixed: float, min_price: float, max_price: float) -> float:
    raw = (cost * (1.0 + margin_percent / 100.0)) + margin_fixed
    clamped = max(min_price, min(max_price, raw))
    return round(clamped + 1e-9, 2)


def resolve_zone_name(shipping_address: dict | None) -> str:
    if not shipping_address:
        return "International"
    country_code = str(shipping_address.get("countryCodeV2") or "").strip().upper()
    country_name = str(shipping_address.get("country") or "").strip().lower()
    if country_code == "FR" or country_name in {"france", "fr"}:
        return "France"
    if country_code in EU_COUNTRY_CODES:
        return "UE (Union Européenne)"
    return "International"


def infer_colis_type(
    colis_types: dict,
    active_colis_type: str | None,
    shipment_weight_kg: float | None,
    package_dims_sorted_cm: list[float] | None,
) -> str | None:
    if not isinstance(colis_types, dict) or not colis_types:
        return active_colis_type
    if shipment_weight_kg is None:
        return active_colis_type

    candidates = []
    for key, definition in colis_types.items():
        if not isinstance(definition, dict):
            continue
        try:
            max_weight = float(definition.get("max_weight_kg"))
        except (TypeError, ValueError):
            continue
        if shipment_weight_kg > max_weight:
            continue
        dims_ok = True
        volume = None
        try:
            dims = normalized_dims(
                float(definition.get("length_cm")),
                float(definition.get("width_cm")),
                float(definition.get("height_cm")),
            )
            volume = dims[0] * dims[1] * dims[2]
            if package_dims_sorted_cm:
                dims_ok = all(dims[index] >= package_dims_sorted_cm[index] for index in range(3))
        except (TypeError, ValueError):
            pass
        if not dims_ok:
            continue
        candidates.append((max_weight, volume if volume is not None else 10**12, str(key)))

    if not candidates:
        return active_colis_type
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def resolve_service_cost(service: dict, colis_type: str | None) -> tuple[float | None, str | None]:
    by_type = service.get("base_cost_by_colis_type")
    if isinstance(by_type, dict) and colis_type and colis_type in by_type:
        try:
            return float(by_type[colis_type]), f"base_cost_by_colis_type[{colis_type}]"
        except (TypeError, ValueError):
            return None, None
    if "base_cost" in service:
        try:
            return float(service["base_cost"]), "base_cost"
        except (TypeError, ValueError):
            return None, None
    return None, None


def build_rate_estimate(
    rates_policy: dict | None,
    shipping_address: dict | None,
    package_choice: dict | None,
    rate_source: str,
    strict_live_rates: bool,
    carrier_timeout_sec: int,
) -> dict | None:
    if not rates_policy:
        return None
    zones = rates_policy.get("zones")
    if not isinstance(zones, list) or not zones:
        return None

    zone_name = resolve_zone_name(shipping_address)
    zone = next((entry for entry in zones if isinstance(entry, dict) and entry.get("zone_name") == zone_name), None)
    if not zone:
        return {
            "zone_name": zone_name,
            "status": "zone_not_configured",
            "services": [],
        }

    shipment_weight = None
    package_dims = None
    if package_choice:
        shipment_weight = package_choice.get("shipment_weight_kg")
        package_dims = package_choice.get("package_dims_sorted_cm")

    inferred_colis_type = infer_colis_type(
        colis_types=rates_policy.get("colis_types") or {},
        active_colis_type=rates_policy.get("active_colis_type"),
        shipment_weight_kg=float(shipment_weight) if shipment_weight is not None else None,
        package_dims_sorted_cm=package_dims if isinstance(package_dims, list) else None,
    )

    services = []
    live_quote_errors: list[dict] = []
    for service in zone.get("services") or []:
        if not isinstance(service, dict):
            continue
        if service.get("active", True) is False:
            continue
        method_name = str(service.get("method_name") or "").strip()
        if not method_name:
            continue
        source_cost, source_mode = resolve_service_cost(service, inferred_colis_type)
        if source_cost is None:
            services.append({"method_name": method_name, "status": "missing_cost"})
            continue
        fallback_cost = source_cost
        effective_cost = source_cost
        effective_currency = str(rates_policy.get("currency_code", "EUR"))
        live_quote = None

        can_try_live = bool(package_choice) and rate_source in {"auto", "live"}
        if can_try_live:
            package = package_choice.get("package") or {}
            live_quote = quote_service_rate(
                service=service,
                parcel={
                    "weight_kg": package_choice.get("shipment_weight_kg"),
                    "length_cm": package.get("inner_length_cm"),
                    "width_cm": package.get("inner_width_cm"),
                    "height_cm": package.get("inner_height_cm"),
                },
                recipient=shipping_address,
                timeout_sec=carrier_timeout_sec,
            )
            live_amount = live_quote.get("amount")
            live_currency = str(live_quote.get("currency_code") or "").strip() or None
            if live_quote.get("status") == "ok" and live_amount is not None:
                if live_currency and live_currency != effective_currency:
                    live_quote = {
                        **live_quote,
                        "status": "currency_mismatch",
                        "message": (
                            f"Live quote currency '{live_currency}' differs from policy currency "
                            f"'{effective_currency}'"
                        ),
                    }
                else:
                    effective_cost = float(live_amount)
                    source_mode = f"live_quote:{live_quote.get('source')}"
            elif rate_source == "live":
                live_quote_errors.append(
                    {
                        "method_name": method_name,
                        "carrier_name": service.get("carrier_name"),
                        "reason": live_quote.get("message") or live_quote.get("status") or "live_quote_failed",
                    }
                )

        margin_percent = float(service.get("margin_percent", rates_policy.get("default_margin_percent", 0.0)))
        margin_fixed = float(service.get("margin_fixed", rates_policy.get("default_margin_fixed", 0.0)))
        min_price = float(service.get("min_price", rates_policy.get("default_min_price", 0.0)))
        max_price = float(service.get("max_price", rates_policy.get("default_max_price", 999999.0)))
        customer_price = compute_price(effective_cost, margin_percent, margin_fixed, min_price, max_price)
        services.append(
            {
                "method_name": method_name,
                "carrier_name": service.get("carrier_name"),
                "carrier_service_code": service.get("carrier_service_code"),
                "carrier_service_name": service.get("carrier_service_name"),
                "source_cost": effective_cost,
                "source_cost_mode": source_mode,
                "fallback_policy_cost": fallback_cost,
                "live_quote": live_quote,
                "provider_purchase_cost": effective_cost,
                "provider_purchase_currency_code": effective_currency,
                "estimated_customer_price": customer_price,
                "currency_code": effective_currency,
            }
        )

    cheapest = None
    valid_services = [entry for entry in services if entry.get("estimated_customer_price") is not None]
    if valid_services:
        cheapest = min(valid_services, key=lambda entry: float(entry["estimated_customer_price"]))

    if strict_live_rates and rate_source == "live" and live_quote_errors:
        return {
            "zone_name": zone_name,
            "colis_type": inferred_colis_type,
            "status": "live_quote_failed",
            "live_quote_errors": live_quote_errors,
            "services": services,
            "recommended_cheapest": None,
        }

    return {
        "zone_name": zone_name,
        "colis_type": inferred_colis_type,
        "rate_source": rate_source,
        "strict_live_rates": bool(strict_live_rates),
        "live_quote_errors": live_quote_errors,
        "services": services,
        "recommended_cheapest": cheapest,
    }


def choose_package(packages: list[dict], items_dims: list[list[float]], total_item_weight_kg: float) -> dict | None:
    if not items_dims:
        return None
    aggregate = [
        max(dims[0] for dims in items_dims),
        max(dims[1] for dims in items_dims),
        sum(dims[2] for dims in items_dims),
    ]
    candidates = []
    for package in packages:
        try:
            package_dims = normalized_dims(
                float(package["inner_length_cm"]),
                float(package["inner_width_cm"]),
                float(package["inner_height_cm"]),
            )
            max_weight = float(package["max_weight_kg"])
            empty_weight = float(package["empty_weight_kg"])
        except (KeyError, TypeError, ValueError):
            continue
        fits_dims = all(package_dims[index] >= aggregate[index] for index in range(3))
        fits_weight = max_weight >= total_item_weight_kg + empty_weight
        if fits_dims and fits_weight:
            volume = package_dims[0] * package_dims[1] * package_dims[2]
            candidates.append((volume, empty_weight, package, package_dims))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    _, empty_weight, package, package_dims = candidates[0]
    shipment_weight = round(total_item_weight_kg + empty_weight, 3)
    return {
        "package": package,
        "package_dims_sorted_cm": package_dims,
        "aggregate_item_dims_sorted_cm": aggregate,
        "shipment_weight_kg": shipment_weight,
    }


def main() -> None:
    args = parse_args()
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    packages, package_source = load_packages(args.packages_file)
    rates_policy, rates_policy_source = load_rates_policy(args.rates_policy_file)
    order = get_order(context, order_gid)

    line_items = order.get("lineItems", {}).get("nodes", [])
    variant_ids = [line["variant"]["id"] for line in line_items if line.get("variant") and line["variant"].get("id")]
    variants = get_variants(context, variant_ids)

    planned_items = []
    missing = []
    total_item_weight_kg = 0.0
    item_dims = []

    for line in line_items:
        quantity = int(line.get("quantity") or 0)
        variant_ref = line.get("variant") or {}
        variant = variants.get(variant_ref.get("id", ""))
        if not variant:
            missing.append({"line_item": line.get("name"), "reason": "missing_variant"})
            continue

        weight_obj = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight") or {}
        shipping_weight_kg = weight_obj.get("value")
        length_cm = decimal_metafield(variant, "parcel_length_cm")
        width_cm = decimal_metafield(variant, "parcel_width_cm")
        height_cm = decimal_metafield(variant, "parcel_height_cm")
        packaging_type = text_metafield(variant, "packaging_type")
        net_weight_kg = decimal_metafield(variant, "net_weight_kg")

        item_missing = []
        if shipping_weight_kg in (None, 0, 0.0):
            item_missing.append("shipping_weight_kg")
        if length_cm is None:
            item_missing.append("parcel_length_cm")
        if width_cm is None:
            item_missing.append("parcel_width_cm")
        if height_cm is None:
            item_missing.append("parcel_height_cm")
        if item_missing:
            missing.append(
                {
                    "line_item": line.get("name"),
                    "variant_id": variant.get("id"),
                    "product_handle": (variant.get("product") or {}).get("handle"),
                    "missing": item_missing,
                }
            )
            continue

        shipping_weight_kg = float(shipping_weight_kg)
        length_cm = float(length_cm)
        width_cm = float(width_cm)
        height_cm = float(height_cm)

        for _ in range(quantity):
            total_item_weight_kg += shipping_weight_kg
            item_dims.append(normalized_dims(length_cm, width_cm, height_cm))

        planned_items.append(
            {
                "line_item_id": line.get("id"),
                "title": line.get("name"),
                "quantity": quantity,
                "sku": line.get("sku"),
                "variant_id": variant.get("id"),
                "product_title": (variant.get("product") or {}).get("title"),
                "product_handle": (variant.get("product") or {}).get("handle"),
                "packaging_type": packaging_type,
                "shipping_weight_kg": shipping_weight_kg,
                "net_weight_kg": net_weight_kg,
                "dimensions_cm": {
                    "length": length_cm,
                    "width": width_cm,
                    "height": height_cm,
                },
            }
        )

    package_choice = choose_package(packages, item_dims, total_item_weight_kg) if not missing else None

    carrier_ready_payload = None
    if package_choice:
        package = package_choice["package"]
        carrier_ready_payload = {
            "recipient": order.get("shippingAddress"),
            "reference": order.get("name"),
            "parcel": {
                "package_code": package.get("code"),
                "package_label": package.get("label"),
                "weight_kg": package_choice["shipment_weight_kg"],
                "length_cm": float(package["inner_length_cm"]),
                "width_cm": float(package["inner_width_cm"]),
                "height_cm": float(package["inner_height_cm"]),
            },
            "line_items": planned_items,
        }

    rate_estimate = None
    if not args.no_rate_estimate:
        rate_estimate = build_rate_estimate(
            rates_policy=rates_policy,
            shipping_address=order.get("shippingAddress"),
            package_choice=package_choice,
            rate_source=args.rate_source,
            strict_live_rates=bool(args.strict_live_rates),
            carrier_timeout_sec=max(1, int(args.carrier_timeout_sec)),
        )
        if rate_estimate and rate_estimate.get("status") == "live_quote_failed":
            fail(
                "Live carrier quote required but unavailable for one or more services: "
                + json.dumps(rate_estimate.get("live_quote_errors") or [], ensure_ascii=True)
            )

    output(
        {
            "ok": True,
            "mode": "plan-carrier-shipment",
            "dry_run": bool(args.dry_run),
            "order": {
                "id": order.get("id"),
                "name": order.get("name"),
                "email": order.get("email"),
                "shipping_address": order.get("shippingAddress"),
            },
            "packages_source": package_source,
            "rates_policy_source": rates_policy_source,
            "rate_source": args.rate_source,
            "strict_live_rates": bool(args.strict_live_rates),
            "summary": {
                "line_count": len(line_items),
                "planned_line_count": len(planned_items),
                "missing_line_count": len(missing),
                "total_item_weight_kg": round(total_item_weight_kg, 3),
            },
            "planned_items": planned_items,
            "missing_requirements": missing,
            "package_choice": package_choice,
            "rate_estimate": rate_estimate,
            "carrier_ready_payload": carrier_ready_payload,
        }
    )


if __name__ == "__main__":
    main()
