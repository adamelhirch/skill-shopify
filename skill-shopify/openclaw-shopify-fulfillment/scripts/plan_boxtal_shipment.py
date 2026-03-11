import argparse
import json
from pathlib import Path

from shopify_admin_ops import fail, graph_ql, output, resolve_context, resolve_order_id


DEFAULT_PACKAGE_FILE = Path(__file__).resolve().parents[1] / "assets" / "package-catalog.example.json"
LOGISTICS_NAMESPACE = "openclaw_logistics"


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
    parser.add_argument("--packages-file", default=str(DEFAULT_PACKAGE_FILE))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_packages(path: str) -> list[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
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
    return valid


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
    packages = load_packages(args.packages_file)
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

    boxtal_payload = None
    if package_choice:
        package = package_choice["package"]
        boxtal_payload = {
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

    output(
        {
            "ok": True,
            "mode": "plan-boxtal-shipment",
            "dry_run": bool(args.dry_run),
            "order": {
                "id": order.get("id"),
                "name": order.get("name"),
                "email": order.get("email"),
                "shipping_address": order.get("shippingAddress"),
            },
            "packages_file": str(Path(args.packages_file).resolve()),
            "summary": {
                "line_count": len(line_items),
                "planned_line_count": len(planned_items),
                "missing_line_count": len(missing),
                "total_item_weight_kg": round(total_item_weight_kg, 3),
            },
            "planned_items": planned_items,
            "missing_requirements": missing,
            "package_choice": package_choice,
            "boxtal_ready_payload": boxtal_payload,
        }
    )


if __name__ == "__main__":
    main()
