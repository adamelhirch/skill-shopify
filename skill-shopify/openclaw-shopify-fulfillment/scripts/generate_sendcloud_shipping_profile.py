import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from shopify_admin_ops import fail, graph_ql, output, resolve_context


DEFAULT_CSV_FILE = Path.home() / "Downloads" / "sendcloud_price_list_20260311_190656.csv"
DEFAULT_POLICY_FILE = Path(__file__).resolve().parents[1] / "assets" / "manual-rate-policy.json"
DEFAULT_ROUTING_FILE = Path(__file__).resolve().parents[1] / "assets" / "sendcloud-routing-policy.json"

HOME_PRICE_MAP = {
    7.67: 7.90,
    8.95: 8.90,
    9.76: 9.90,
    11.10: 10.90,
    11.66: 11.90,
    12.07: 11.90,
    12.13: 11.90,
    12.86: 12.90,
    13.42: 13.49,
    13.48: 13.49,
    13.82: 13.90,
    13.84: 13.90,
    15.76: 15.90,
    16.41: 16.49,
    19.52: 19.49,
    19.98: 19.90,
    21.20: 21.90,
    21.47: 21.90,
    22.13: 21.90,
    24.85: 24.90,
    25.89: 25.90,
    27.74: 27.49,
    30.12: 29.90,
    34.70: 34.49,
    35.32: 35.49,
}

RELAY_PRICE_MAP = {
    3.29: 3.49,
    4.15: 3.90,
    4.80: 4.90,
    4.96: 4.90,
    5.84: 5.90,
    6.00: 5.90,
    6.21: 6.49,
    8.67: 8.90,
    9.90: 9.90,
    10.88: 10.90,
    11.26: 11.49,
    13.72: 13.49,
}

FRANCE_SERVICES = [
    {
        "method_name": "Livraison en Point Relais",
        "description": "Choisissez votre point relais apres paiement",
        "base_cost": 3.31,
        "min_price": 3.90,
        "max_price": 3.90,
        "carrier_name": "Openclaw Routed",
        "carrier_service_name": "France relay dynamic",
        "carrier_service_code": "FR_RELAY_DYNAMIC",
        "active": True,
    },
    {
        "method_name": "Livraison à domicile",
        "description": "2 a 5 jours via Mondial Relay ou Colissimo",
        "base_cost": 7.67,
        "min_price": 7.49,
        "max_price": 7.49,
        "carrier_name": "Openclaw Routed",
        "carrier_service_name": "France home blended dynamic",
        "carrier_service_code": "FR_HOME_DYNAMIC",
        "active": True,
    },
]

COLIS_TYPES = {
    "S": {
        "label": "Petit colis",
        "max_weight_kg": 0.35,
        "length_cm": 18,
        "width_cm": 12,
        "height_cm": 4,
    },
    "M": {
        "label": "Colis moyen",
        "max_weight_kg": 1.0,
        "length_cm": 22,
        "width_cm": 16,
        "height_cm": 8,
    },
    "L": {
        "label": "Grand colis",
        "max_weight_kg": 3.0,
        "length_cm": 32,
        "width_cm": 24,
        "height_cm": 12,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an Openclaw Shopify shipping profile from a Sendcloud CSV export.")
    parser.add_argument("--csv-file", default=str(DEFAULT_CSV_FILE))
    parser.add_argument("--policy-output", default=str(DEFAULT_POLICY_FILE))
    parser.add_argument("--routing-output", default=str(DEFAULT_ROUTING_FILE))
    parser.add_argument("--profile-name", default="Openclaw Shipping")
    parser.add_argument("--market-name", default="International")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [row for row in reader]
    except OSError as exc:
        fail(f"Unable to read CSV file: {exc}")


def csv_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def parse_decimal(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return round(float(raw.replace(",", ".")), 2)
    except ValueError:
        return None


def bucket_cost(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def active_market_country_codes(context: dict) -> set[str]:
    data = graph_ql(
        context,
        """
        query ShippingProfileGeneratorMarkets($first: Int!) {
          markets(first: $first) {
            nodes {
              id
              name
              regions(first: 250) {
                nodes {
                  __typename
                  ... on MarketRegionCountry {
                    code
                  }
                }
              }
            }
          }
        }
        """,
        {"first": 100},
    )
    country_codes: set[str] = set()
    markets = []
    for market in data.get("markets", {}).get("nodes", []):
        market_name = str(market.get("name") or "").strip()
        codes = []
        for region in market.get("regions", {}).get("nodes", []):
            if region.get("__typename") != "MarketRegionCountry":
                continue
            code = str(region.get("code") or "").strip().upper()
            if not code:
                continue
            country_codes.add(code)
            codes.append(code)
        markets.append({"name": market_name, "country_codes": sorted(codes)})
    return country_codes


def collect_cheapest_routes(rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    cheapest_home: dict[str, dict] = {}
    cheapest_relay: dict[str, dict] = {}
    ignored_countries: list[str] = []

    for row in rows:
        if str(row.get("From country") or "").strip().upper() != "FR":
            continue
        if not csv_bool(row.get("B2C")):
            continue
        if str(row.get("Form factor") or "").strip().lower() != "parcel":
            continue

        min_weight = parse_decimal(row.get("Minimum weight"))
        max_weight = parse_decimal(row.get("Maximum weight"))
        total_price = parse_decimal(row.get("Total price"))
        if min_weight is None or max_weight is None or total_price is None:
            continue
        if not (min_weight <= 0.0011 and max_weight <= 0.501):
            continue

        country_code = str(row.get("To country") or "").strip().upper()
        if not country_code or country_code == "FR":
            continue

        route = {
            "country_code": country_code,
            "cost": bucket_cost(total_price),
            "carrier": str(row.get("Carrier code") or "").strip(),
            "name": str(row.get("Shipping method friendly name") or "").strip(),
            "code": str(row.get("Shipping method code") or "").strip(),
            "last_mile": str(row.get("Last mile") or "").strip().lower(),
        }
        target = None
        if route["last_mile"] == "home_delivery":
            target = cheapest_home
        elif route["last_mile"] in {"service_point", "locker"}:
            target = cheapest_relay
        else:
            if country_code not in ignored_countries:
                ignored_countries.append(country_code)
            continue

        current = target.get(country_code)
        if current is None or route["cost"] < current["cost"]:
            target[country_code] = route

    return cheapest_home, cheapest_relay, sorted(ignored_countries)


def build_service(method_name: str, description: str, actual_cost: float, public_price: float, service_code: str) -> dict:
    return {
        "method_name": method_name,
        "description": description,
        "base_cost": bucket_cost(actual_cost),
        "min_price": bucket_cost(public_price),
        "max_price": bucket_cost(public_price),
        "carrier_name": "Openclaw Routed",
        "carrier_service_name": method_name,
        "carrier_service_code": service_code,
        "active": True,
    }


def zone_name_for_prices(home_price: float, relay_price: float | None) -> str:
    if relay_price is None:
        return f"INT DOM {home_price:.2f}"
    return f"INT DOM {home_price:.2f} RELAIS {relay_price:.2f}"


def build_generated_policy(
    profile_name: str,
    cheapest_home: dict[str, dict],
    cheapest_relay: dict[str, dict],
    allowed_country_codes: set[str] | None,
) -> tuple[dict, dict]:
    grouped: dict[tuple[float, float | None], dict] = defaultdict(lambda: {"countries": [], "home_costs": [], "relay_costs": []})
    skipped_missing_home: list[str] = []
    skipped_home_bucket: dict[str, float] = {}
    skipped_relay_bucket: dict[str, float] = {}
    filtered_out_market: list[str] = []
    relay_countries: list[str] = []
    home_countries: list[str] = []

    for country_code in sorted(set(cheapest_home) | set(cheapest_relay)):
        if allowed_country_codes is not None and country_code not in allowed_country_codes:
            filtered_out_market.append(country_code)
            continue

        home = cheapest_home.get(country_code)
        if not home:
            skipped_missing_home.append(country_code)
            continue

        home_public = HOME_PRICE_MAP.get(home["cost"])
        if home_public is None:
            skipped_home_bucket[country_code] = home["cost"]
            continue

        relay = cheapest_relay.get(country_code)
        relay_public = None
        relay_cost = None
        if relay:
            relay_public = RELAY_PRICE_MAP.get(relay["cost"])
            if relay_public is None:
                skipped_relay_bucket[country_code] = relay["cost"]
            else:
                relay_cost = relay["cost"]
                relay_countries.append(country_code)

        grouped[(home_public, relay_public)]["countries"].append(country_code)
        grouped[(home_public, relay_public)]["home_costs"].append(home["cost"])
        if relay_cost is not None:
            grouped[(home_public, relay_public)]["relay_costs"].append(relay_cost)
        home_countries.append(country_code)

    zones = [
        {
            "zone_name": "France",
            "country_codes": ["FR"],
            "services": FRANCE_SERVICES,
        }
    ]
    for (home_price, relay_price), values in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1] or 0.0)):
        zone_services = [
            build_service(
                method_name="Livraison à domicile",
                description="Delais variables selon destination",
                actual_cost=max(values["home_costs"]),
                public_price=home_price,
                service_code=f"INT_DOM_{home_price:.2f}",
            )
        ]
        if relay_price is not None and values["relay_costs"]:
            zone_services.append(
                build_service(
                    method_name="Livraison en Point Relais",
                    description="Choisissez votre point relais apres paiement",
                    actual_cost=max(values["relay_costs"]),
                    public_price=relay_price,
                    service_code=f"INT_RELAY_{relay_price:.2f}",
                )
            )
        zones.append(
            {
                "zone_name": zone_name_for_prices(home_price, relay_price),
                "country_codes": values["countries"],
                "services": zone_services,
            }
        )

    policy = {
        "currency_code": "EUR",
        "default_margin_percent": 0.0,
        "default_margin_fixed": 0.0,
        "default_min_price": 0.0,
        "default_max_price": 999.0,
        "delete_unmanaged": True,
        "features": {
            "pickup_point_checkout": False,
            "checkout_dynamic_rates": False,
            "live_carrier_quote_in_checkout": False,
        },
        "active_colis_type": "M",
        "colis_types": COLIS_TYPES,
        "profiles": [
            {
                "profile_name": profile_name,
                "create_if_missing": True,
                "delete_unmanaged": True,
                "zones": zones,
            }
        ],
    }

    routing_rules = [
        {
            "country_codes": ["FR"],
            "checkout_method_names": ["Livraison en Point Relais"],
            "selection_strategy": "priority",
            "families": [
                {
                    "carrier": "mondial_relay",
                    "name_contains_any": ["Point Relais"],
                    "service_point_input": "required",
                },
                {
                    "carrier": "chronopost",
                    "name_contains_any": ["Shop2Shop"],
                    "service_point_input": "required",
                },
            ],
        },
        {
            "country_codes": ["FR"],
            "checkout_method_names": ["Livraison à domicile"],
            "selection_strategy": "priority",
            "families": [
                {
                    "carrier": "mondial_relay",
                    "name_contains_any": ["Home Domestic"],
                    "service_point_input": "none",
                },
                {
                    "carrier": "colissimo",
                    "name_contains_any": ["Colissimo Home"],
                    "name_excludes_any": ["Signature"],
                    "service_point_input": "none",
                }
            ],
        },
    ]

    international_home_countries = sorted(code for code in home_countries if code != "FR")
    if international_home_countries:
        routing_rules.append(
            {
                "country_codes": international_home_countries,
                "checkout_method_names": ["Livraison à domicile"],
                "selection_strategy": "cheapest",
                "families": [
                    {
                        "carrier": "fedex",
                        "name_contains_any": ["International Connect Plus"],
                        "service_point_input": "none",
                    },
                    {
                        "carrier": "colissimo",
                        "name_contains_any": ["Domicile International sans Signature", "Europe without Signature"],
                        "service_point_input": "none",
                    },
                    {
                        "carrier": "colissimo",
                        "name_contains_any": ["Domicile International avec Signature", "Europe with Signature", "Colissimo Home"],
                        "service_point_input": "none",
                    },
                ],
            }
        )

    relay_enabled_countries = sorted(code for code in relay_countries if code != "FR")
    if relay_enabled_countries:
        routing_rules.append(
            {
                "country_codes": relay_enabled_countries,
                "checkout_method_names": ["Livraison en Point Relais"],
                "selection_strategy": "cheapest",
                "families": [
                    {
                        "carrier": "chronopost",
                        "name_contains_any": ["2Shop Europe", "Shop2Shop"],
                        "service_point_input": "required",
                    },
                    {
                        "carrier": "colissimo",
                        "name_contains_any": ["Colissimo Service Point", "Colissimo Europe Service Point", "Point Retrait International"],
                        "service_point_input": "required",
                    },
                ],
            }
        )

    routing_policy = {
        "version": 1,
        "selection_defaults": {
            "require_positive_price": True,
            "maximum_price": 999.0,
        },
        "rules": routing_rules,
    }

    report = {
        "home_country_count": len(home_countries),
        "relay_country_count": len(relay_countries),
        "zone_count": len(zones),
        "filtered_out_market": filtered_out_market,
        "skipped_missing_home": skipped_missing_home,
        "skipped_home_bucket": skipped_home_bucket,
        "skipped_relay_bucket": skipped_relay_bucket,
    }
    return policy, {"routing_policy": routing_policy, "report": report}


def write_json(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        fail(f"Unable to write {path}: {exc}")


def main() -> None:
    args = parse_args()
    csv_file = Path(args.csv_file).expanduser()
    if not csv_file.exists():
        fail(f"CSV file not found: {csv_file}")

    rows = read_csv_rows(csv_file)
    cheapest_home, cheapest_relay, ignored_countries = collect_cheapest_routes(rows)

    market_country_codes = None
    if args.store or args.shop_url or args.token or args.client_id or args.client_secret:
        context = resolve_context(args)
        market_country_codes = active_market_country_codes(context)

    policy, routing_bundle = build_generated_policy(
        profile_name=args.profile_name,
        cheapest_home=cheapest_home,
        cheapest_relay=cheapest_relay,
        allowed_country_codes=market_country_codes,
    )

    report = dict(routing_bundle["report"])
    if market_country_codes is not None:
        priced_country_codes = sorted(set(cheapest_home) | set(cheapest_relay))
        report["active_market_country_count"] = len(market_country_codes)
        report["missing_market_country_codes"] = sorted(code for code in priced_country_codes if code not in market_country_codes)
    report["ignored_countries"] = ignored_countries
    report["csv_file"] = str(csv_file.resolve())
    report["policy_output"] = str(Path(args.policy_output).resolve())
    report["routing_output"] = str(Path(args.routing_output).resolve())

    if not args.dry_run:
        write_json(Path(args.policy_output), policy)
        write_json(Path(args.routing_output), routing_bundle["routing_policy"])

    output(
        {
            "ok": True,
            "mode": "generate-sendcloud-shipping-profile",
            "dry_run": bool(args.dry_run),
            "report": report,
            "policy": policy,
            "routing_policy": routing_bundle["routing_policy"],
        }
    )


if __name__ == "__main__":
    main()
