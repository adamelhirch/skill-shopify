import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

from shopify_admin_ops import graph_ql, output, resolve_context


DEFAULT_CSV_OUTPUT = Path("output/spreadsheet/shopify_variant_weight_estimates.csv")
DEFAULT_JSON_OUTPUT = Path("output/spreadsheet/shopify_variant_weight_estimates.summary.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate Shopify variant shipping weights from catalog metadata.")
    parser.add_argument("--csv-output", default=str(DEFAULT_CSV_OUTPUT))
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--no-output", action="store_true", help="Do not write CSV/JSON artifacts.")
    parser.add_argument("--apply", action="store_true", help="Apply estimated shipping weights to Shopify inventory items.")
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    return parser.parse_args()


def normalize_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return raw.casefold()


def round_kg(value: float) -> float:
    return round(float(value) + 1e-9, 3)


def fetch_variants(context: dict) -> list[dict]:
    after_cursor = None
    variants: list[dict] = []
    while True:
        data = graph_ql(
            context,
            """
            query WeightEstimateVariants($first: Int!, $after: String) {
              productVariants(first: $first, after: $after) {
                nodes {
                  id
                  sku
                  title
                  inventoryItem {
                    id
                    tracked
                    requiresShipping
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
                    productType
                    tags
                    status
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
            """,
            {"first": 100, "after": after_cursor},
        )
        page = data.get("productVariants", {})
        for node in page.get("nodes") or []:
            inventory_item = node.get("inventoryItem") or {}
            if inventory_item.get("requiresShipping") is not True:
                continue
            variants.append(node)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after_cursor = page_info.get("endCursor")
        if not after_cursor:
            break
    return variants


def density_for_text(normalized: str) -> float:
    if "huile essentielle" in normalized:
        return 0.9
    if "huile" in normalized:
        return 0.92
    return 1.0


def extract_measurements_kg(product_title: str, variant_title: str) -> tuple[list[float], list[str]]:
    combined = f"{product_title} {variant_title}"
    normalized = normalize_text(combined)
    density = density_for_text(normalized)
    matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml)\b", normalized)
    values_kg: list[float] = []
    reasons: list[str] = []
    for raw_value, unit in matches:
        value = float(raw_value.replace(",", "."))
        if unit == "kg":
            kg = value
        elif unit == "g":
            kg = value / 1000.0
        else:
            kg = (value * density) / 1000.0
        values_kg.append(kg)
        reasons.append(f"{value:g}{unit}")
    return values_kg, reasons


def infer_vanilla_net_weight_kg(product_title: str, variant_title: str) -> tuple[float | None, str | None]:
    normalized_product = normalize_text(product_title)
    normalized_variant = normalize_text(variant_title)
    if "gousse" not in normalized_product and "gousse" not in normalized_variant:
        return None, None

    count_match = re.search(r"(\d+)\s*gousses?", normalized_variant) or re.search(r"(\d+)\s*gousses?", normalized_product)
    if not count_match:
        return None, None
    count = int(count_match.group(1))

    grams_per_gousse = 4.0
    if "pompona" in normalized_product or "xxl" in normalized_product:
        grams_per_gousse = 7.0
    elif normalized_variant.startswith("s "):
        grams_per_gousse = 3.0
    elif normalized_variant.startswith("m "):
        grams_per_gousse = 4.0
    elif normalized_variant.startswith("l "):
        grams_per_gousse = 5.0
    elif normalized_variant.startswith("xl "):
        grams_per_gousse = 6.0

    net_kg = (count * grams_per_gousse) / 1000.0
    return round_kg(net_kg), f"{count} gousses x {grams_per_gousse:g}g"


def count_multiplier(normalized: str, token: str) -> int:
    match = re.search(rf"(\d+)\s+{token}s?\b", normalized)
    if match:
        return int(match.group(1))
    return 1 if token in normalized else 0


def infer_packaging_tare_kg(product_title: str, variant_title: str, net_kg: float) -> tuple[float, str]:
    combined = f"{product_title} {variant_title}"
    normalized = normalize_text(combined)

    if "moulin" in normalized and "+" in normalized:
        sachets = count_multiplier(normalized, "sachet")
        moulins = count_multiplier(normalized, "moulin")
        tare = (sachets * 0.006) + (moulins * 0.065)
        return round_kg(tare), f"{sachets}x sachet + {moulins}x moulin"

    if "pot en verre" in normalized:
        pots = count_multiplier(normalized, "pot")
        return round_kg(max(1, pots) * 0.09), "pot en verre"

    if "pot a epices" in normalized or "pot a epice" in normalized or "pot à épices" in combined or "pot à épice" in combined:
        pots = count_multiplier(normalized, "pot")
        return round_kg(max(1, pots) * 0.05), "pot à épices"

    if "moulin" in normalized:
        moulins = count_multiplier(normalized, "moulin")
        return round_kg(max(1, moulins) * 0.065), "moulin"

    if "flacon" in normalized or "huile essentielle" in normalized or "extrait" in normalized:
        if "100ml" in normalized or "100 ml" in normalized:
            return 0.08, "flacon 100mL"
        if "20ml" in normalized or "20 ml" in normalized:
            return 0.03, "flacon 20mL"
        if "5ml" in normalized or "5 ml" in normalized:
            return 0.02, "flacon 5mL"
        return 0.04, "flacon"

    if "sachet" in normalized:
        if net_kg >= 0.5:
            return 0.015, "grand sachet"
        if net_kg >= 0.1:
            return 0.01, "sachet moyen"
        return 0.006, "petit sachet"

    if "achard" in normalized or "legumes" in normalized or "légumes" in combined:
        return 0.18, "bocal moyen"

    if "pate de piment" in normalized or "pâte de piment" in combined:
        return 0.12, "bocal piment"

    if "caviar" in normalized:
        return 0.055, "petit pot verre"

    if "biscuits" in normalized:
        return 0.015, "etui biscuits"

    if "bonbons coco" in normalized:
        if net_kg >= 0.5:
            return 0.015, "sachet bonbons grand"
        return 0.01, "sachet bonbons"

    if "sel de camargue" in normalized or "sel " in normalized:
        return 0.01, "sachet sel"

    if "pack" in normalized:
        return 0.02, "pack multi-produits"

    if "gousse" in normalized:
        if net_kg >= 0.2:
            return 0.015, "sachet vanille grand format"
        return 0.006, "sachet vanille"

    return 0.02, "fallback"


def infer_default_net_weight_kg(product_title: str, variant_title: str) -> tuple[float, str]:
    combined = f"{product_title} {variant_title}"
    normalized = normalize_text(combined)

    if "sel de camargue" in normalized or "sel " in normalized:
        return 0.10, "default sel 100g"
    if "pate de piment" in normalized or "pâte de piment" in combined:
        return 0.10, "default pâte 100g"
    if "pack decouverte epices" in normalized or "pack découverte épices" in combined:
        return 0.18, "default pack découverte"
    if "pack rhum arrange" in normalized or "pack rhum arrangé" in combined:
        return 0.08, "default pack rhum arrangé"
    if "pack patisserie premium" in normalized or "pack pâtisserie premium" in combined:
        return 0.22, "default pack pâtisserie premium"
    if "pack patisserie decouverte" in normalized or "pack pâtisserie découverte" in combined:
        return 0.12, "default pack pâtisserie découverte"
    if "pack patisserie" in normalized or "pack pâtisserie" in combined:
        return 0.11, "default pack pâtisserie"
    return 0.10, "default net 100g"


def estimate_variant_weight(record: dict) -> dict:
    product = record.get("product") or {}
    inventory_item = record.get("inventoryItem") or {}
    product_title = str(product.get("title") or "").strip()
    variant_title = str(record.get("title") or "").strip()
    current_weight = (((inventory_item.get("measurement") or {}).get("weight") or {}).get("value"))
    current_weight_kg = round_kg(float(current_weight)) if current_weight not in (None, "") else 0.0

    measurements_kg, measurement_reasons = extract_measurements_kg(product_title, variant_title)
    vanilla_net_kg, vanilla_reason = infer_vanilla_net_weight_kg(product_title, variant_title)

    confidence = "medium"
    if measurements_kg:
        net_kg = round_kg(sum(measurements_kg))
        net_reason = " + ".join(measurement_reasons)
        confidence = "high"
    elif vanilla_net_kg is not None:
        net_kg = vanilla_net_kg
        net_reason = vanilla_reason or "vanille rule"
        confidence = "medium"
    else:
        net_kg, net_reason = infer_default_net_weight_kg(product_title, variant_title)
        confidence = "low"

    tare_kg, packaging_reason = infer_packaging_tare_kg(product_title, variant_title, net_kg)
    estimated_weight_kg = round_kg(net_kg + tare_kg)

    if current_weight_kg > 0 and abs(current_weight_kg - estimated_weight_kg) <= 0.01:
        confidence = "high"
    elif current_weight_kg > 0 and abs(current_weight_kg - estimated_weight_kg) > 0.05:
        confidence = "review"

    return {
        "variant_id": record.get("id"),
        "product_id": product.get("id"),
        "status": product.get("status"),
        "handle": product.get("handle"),
        "sku": record.get("sku"),
        "product_title": product_title,
        "variant_title": variant_title,
        "current_weight_kg": current_weight_kg,
        "estimated_net_weight_kg": net_kg,
        "estimated_packaging_tare_kg": tare_kg,
        "estimated_shipping_weight_kg": estimated_weight_kg,
        "confidence": confidence,
        "net_basis": net_reason,
        "packaging_basis": packaging_reason,
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_parent(path)
    fieldnames = [
        "variant_id",
        "product_id",
        "status",
        "handle",
        "sku",
        "product_title",
        "variant_title",
        "current_weight_kg",
        "estimated_net_weight_kg",
        "estimated_packaging_tare_kg",
        "estimated_shipping_weight_kg",
        "confidence",
        "net_basis",
        "packaging_basis",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, rows: list[dict]) -> dict:
    ensure_parent(path)
    existing = sum(1 for row in rows if float(row["current_weight_kg"] or 0) > 0)
    missing = len(rows) - existing
    confidence_counts: dict[str, int] = {}
    for row in rows:
        confidence = str(row.get("confidence") or "unknown")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    summary = {
        "variant_count": len(rows),
        "existing_weight_count": existing,
        "missing_weight_count": missing,
        "confidence_counts": confidence_counts,
        "review_variants": [row for row in rows if row.get("confidence") in {"low", "review"}][:20],
    }
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def build_summary(rows: list[dict]) -> dict:
    existing = sum(1 for row in rows if float(row["current_weight_kg"] or 0) > 0)
    missing = len(rows) - existing
    confidence_counts: dict[str, int] = {}
    for row in rows:
        confidence = str(row.get("confidence") or "unknown")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    return {
        "variant_count": len(rows),
        "existing_weight_count": existing,
        "missing_weight_count": missing,
        "confidence_counts": confidence_counts,
        "review_variants": [row for row in rows if row.get("confidence") in {"low", "review"}][:20],
    }


def apply_estimated_weights(context: dict, records: list[dict], rows: list[dict]) -> dict:
    rows_by_variant_id = {str(row["variant_id"]): row for row in rows}
    mutation = """
    mutation InventoryItemWeightUpdate($id: ID!, $input: InventoryItemInput!) {
      inventoryItemUpdate(id: $id, input: $input) {
        inventoryItem {
          id
          measurement {
            weight {
              value
              unit
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    updated = 0
    skipped = 0
    failed: list[dict] = []
    applied: list[dict] = []
    for record in records:
        variant_id = str(record.get("id") or "")
        row = rows_by_variant_id.get(variant_id)
        if not row:
            continue
        current_weight_kg = float(row.get("current_weight_kg") or 0.0)
        estimated_weight_kg = float(row.get("estimated_shipping_weight_kg") or 0.0)
        if abs(current_weight_kg - estimated_weight_kg) <= 0.001:
            skipped += 1
            continue
        inventory_item = record.get("inventoryItem") or {}
        inventory_item_id = inventory_item.get("id")
        if not inventory_item_id:
            failed.append(
                {
                    "variant_id": variant_id,
                    "product_title": row.get("product_title"),
                    "variant_title": row.get("variant_title"),
                    "reason": "Missing inventory item id",
                }
            )
            continue
        payload = graph_ql(
            context,
            mutation,
            {
                "id": inventory_item_id,
                "input": {
                    "measurement": {
                        "weight": {
                            "value": estimated_weight_kg,
                            "unit": "KILOGRAMS",
                        }
                    }
                },
            },
        ).get("inventoryItemUpdate") or {}
        user_errors = payload.get("userErrors") or []
        if user_errors:
            failed.append(
                {
                    "variant_id": variant_id,
                    "product_title": row.get("product_title"),
                    "variant_title": row.get("variant_title"),
                    "estimated_weight_kg": estimated_weight_kg,
                    "user_errors": user_errors,
                }
            )
            continue
        updated += 1
        applied.append(
            {
                "variant_id": variant_id,
                "product_title": row.get("product_title"),
                "variant_title": row.get("variant_title"),
                "from_weight_kg": current_weight_kg,
                "to_weight_kg": estimated_weight_kg,
                "confidence": row.get("confidence"),
            }
        )
    return {
        "updated_count": updated,
        "skipped_count": skipped,
        "failed_count": len(failed),
        "failed": failed,
        "sample_updates": applied[:20],
    }


def main() -> None:
    args = parse_args()
    context = resolve_context(args)
    variants = fetch_variants(context)
    rows = [estimate_variant_weight(record) for record in variants]
    rows.sort(key=lambda row: (str(row.get("status") or ""), str(row.get("product_title") or ""), str(row.get("variant_title") or "")))
    summary = build_summary(rows)
    if not args.no_output:
        csv_path = Path(args.csv_output)
        json_path = Path(args.json_output)
        write_csv(csv_path, rows)
        write_summary(json_path, rows)
        payload = {
            "ok": True,
            "mode": "estimate-shopify-variant-weights",
            "csv_output": str(csv_path.resolve()),
            "json_output": str(json_path.resolve()),
            "summary": summary,
        }
    else:
        payload = {
            "ok": True,
            "mode": "estimate-shopify-variant-weights",
            "summary": summary,
        }
    if args.apply:
        payload["apply"] = apply_estimated_weights(context, variants, rows)
    output(payload)


if __name__ == "__main__":
    main()
