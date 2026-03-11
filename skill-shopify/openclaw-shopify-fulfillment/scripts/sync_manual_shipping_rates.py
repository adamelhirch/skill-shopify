import argparse
import json
from pathlib import Path

from shopify_admin_ops import fail, graph_ql, output, resolve_context


DEFAULT_POLICY_FILE = Path(__file__).resolve().parents[1] / "assets" / "manual-rate-policy.example.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync manual Shopify delivery rates by zone/service using margin rules."
    )
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    parser.add_argument("--profile-id")
    parser.add_argument("--profile-name", default="General profile")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY_FILE))
    parser.add_argument("--costs-file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-unmanaged", action="store_true")
    return parser.parse_args()


def read_json(path: str, label: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"Unable to read {label}: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON in {label}: {exc}")


def parse_cost_overrides(path: str | None) -> dict[tuple[str, str], float]:
    if not path:
        return {}
    payload = read_json(path, "costs-file")
    rows = payload.get("costs")
    if not isinstance(rows, list):
        fail("costs-file must contain a costs array")
    result: dict[tuple[str, str], float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        zone_name = str(row.get("zone_name") or "").strip()
        service_code = str(row.get("service_code") or "").strip()
        if not zone_name or not service_code:
            continue
        try:
            cost = float(row["cost"])
        except (KeyError, TypeError, ValueError):
            continue
        result[(zone_name, service_code)] = cost
    return result


def compute_price(cost: float, margin_percent: float, margin_fixed: float, min_price: float, max_price: float) -> float:
    raw = (cost * (1.0 + margin_percent / 100.0)) + margin_fixed
    clamped = max(min_price, min(max_price, raw))
    return round(clamped + 1e-9, 2)


def get_profiles(context: dict) -> list[dict]:
    data = graph_ql(
        context,
        """
        query DeliveryProfilesForSync {
          deliveryProfiles(first: 5) {
            nodes {
              id
              name
              profileLocationGroups {
                locationGroup {
                  id
                }
                locationGroupZones(first: 30) {
                  nodes {
                    zone {
                      id
                      name
                    }
                    methodDefinitions(first: 30) {
                      nodes {
                        id
                        name
                        active
                        rateProvider {
                          ... on DeliveryRateDefinition {
                            id
                            price {
                              amount
                              currencyCode
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """,
    )
    return data.get("deliveryProfiles", {}).get("nodes", [])


def find_profile(profiles: list[dict], profile_id: str | None, profile_name: str) -> dict:
    if profile_id:
        for profile in profiles:
            if profile.get("id") == profile_id:
                return profile
        fail(f"Profile not found by id: {profile_id}")
    for profile in profiles:
        if profile.get("name") == profile_name:
            return profile
    fail(f"Profile not found by name: {profile_name}")


def build_zone_updates(profile: dict, policy: dict, costs: dict[tuple[str, str], float], delete_unmanaged: bool) -> tuple[list[dict], list[dict]]:
    currency_code = str(policy.get("currency_code") or "EUR")
    default_margin_percent = float(policy.get("default_margin_percent", 0.0))
    default_margin_fixed = float(policy.get("default_margin_fixed", 0.0))
    default_min_price = float(policy.get("default_min_price", 0.0))
    default_max_price = float(policy.get("default_max_price", 999999.0))
    zone_policies = policy.get("zones")
    if not isinstance(zone_policies, list) or not zone_policies:
        fail("policy-file must contain a non-empty zones array")

    report = []
    location_group_updates = []

    for group in profile.get("profileLocationGroups", []):
        group_id = group.get("locationGroup", {}).get("id")
        if not group_id:
            continue
        zone_updates = []
        zones = group.get("locationGroupZones", {}).get("nodes", [])
        zones_by_name = {
            (zone_node.get("zone", {}).get("name") or ""): zone_node
            for zone_node in zones
        }

        for zone_policy in zone_policies:
            if not isinstance(zone_policy, dict):
                continue
            zone_name = str(zone_policy.get("zone_name") or "").strip()
            services = zone_policy.get("services")
            if not zone_name or not isinstance(services, list):
                continue
            zone_node = zones_by_name.get(zone_name)
            if not zone_node:
                report.append(
                    {
                        "zone_name": zone_name,
                        "status": "zone_missing",
                        "message": "Zone not found in delivery profile",
                    }
                )
                continue

            existing_methods = zone_node.get("methodDefinitions", {}).get("nodes", [])
            existing_by_name = {
                (method.get("name") or "").strip(): method
                for method in existing_methods
                if method.get("name")
            }
            managed_names = set()
            methods_to_create = []
            methods_to_update = []
            methods_to_delete = []
            service_report = []

            for service in services:
                if not isinstance(service, dict):
                    continue
                if service.get("active", True) is False:
                    continue
                method_name = str(service.get("method_name") or "").strip()
                service_code = str(service.get("service_code") or "").strip()
                if not method_name or not service_code:
                    continue
                managed_names.add(method_name)

                cost_key = (zone_name, service_code)
                source_cost = costs.get(cost_key)
                if source_cost is None:
                    try:
                        source_cost = float(service["base_cost"])
                    except (KeyError, TypeError, ValueError):
                        service_report.append(
                            {
                                "service_code": service_code,
                                "method_name": method_name,
                                "status": "missing_cost",
                            }
                        )
                        continue

                margin_percent = float(service.get("margin_percent", default_margin_percent))
                margin_fixed = float(service.get("margin_fixed", default_margin_fixed))
                min_price = float(service.get("min_price", default_min_price))
                max_price = float(service.get("max_price", default_max_price))
                price = compute_price(source_cost, margin_percent, margin_fixed, min_price, max_price)

                existing = existing_by_name.get(method_name)
                if existing:
                    rate_provider = existing.get("rateProvider") or {}
                    rate_id = rate_provider.get("id")
                    current_price_raw = (rate_provider.get("price") or {}).get("amount")
                    current_price = float(current_price_raw) if current_price_raw is not None else None
                    update_payload = {
                        "id": existing.get("id"),
                        "name": method_name,
                        "active": True,
                        "rateDefinition": {
                            "price": {
                                "amount": str(price),
                                "currencyCode": currency_code,
                            }
                        },
                    }
                    if rate_id:
                        update_payload["rateDefinition"]["id"] = rate_id
                    methods_to_update.append(update_payload)
                    service_report.append(
                        {
                            "service_code": service_code,
                            "method_name": method_name,
                            "status": "update",
                            "source_cost": source_cost,
                            "target_price": price,
                            "current_price": current_price,
                        }
                    )
                else:
                    methods_to_create.append(
                        {
                            "name": method_name,
                            "active": True,
                            "rateDefinition": {
                                "price": {
                                    "amount": str(price),
                                    "currencyCode": currency_code,
                                }
                            },
                        }
                    )
                    service_report.append(
                        {
                            "service_code": service_code,
                            "method_name": method_name,
                            "status": "create",
                            "source_cost": source_cost,
                            "target_price": price,
                        }
                    )

            if delete_unmanaged:
                for method in existing_methods:
                    method_name = (method.get("name") or "").strip()
                    if not method_name:
                        continue
                    if method_name in managed_names:
                        continue
                    methods_to_delete.append(method.get("id"))
                    service_report.append(
                        {
                            "service_code": None,
                            "method_name": method_name,
                            "status": "delete",
                        }
                    )

            zone_update = {"id": zone_node.get("zone", {}).get("id")}
            if methods_to_create:
                zone_update["methodDefinitionsToCreate"] = methods_to_create
            if methods_to_update:
                zone_update["methodDefinitionsToUpdate"] = methods_to_update
            if methods_to_create or methods_to_update:
                zone_updates.append(zone_update)

            report.append(
                {
                    "zone_name": zone_name,
                    "zone_id": zone_node.get("zone", {}).get("id"),
                    "methods": service_report,
                    "method_definitions_to_delete": methods_to_delete,
                }
            )

        if zone_updates:
            location_group_updates.append({"id": group_id, "zonesToUpdate": zone_updates})

    return location_group_updates, report


def run_update(context: dict, profile_id: str, profile_input: dict) -> dict:
    return graph_ql(
        context,
        """
        mutation SyncManualRates($id: ID!, $profile: DeliveryProfileInput!) {
          deliveryProfileUpdate(id: $id, profile: $profile) {
            profile {
              id
              name
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"id": profile_id, "profile": profile_input},
    )


def main() -> None:
    args = parse_args()
    context = resolve_context(args)
    policy = read_json(args.policy_file, "policy-file")
    cost_overrides = parse_cost_overrides(args.costs_file)
    profiles = get_profiles(context)
    profile = find_profile(profiles, args.profile_id, args.profile_name)

    location_group_updates, report = build_zone_updates(
        profile=profile,
        policy=policy,
        costs=cost_overrides,
        delete_unmanaged=args.delete_unmanaged,
    )

    profile_input = {}
    if location_group_updates:
        profile_input["locationGroupsToUpdate"] = location_group_updates

    response = None
    if args.apply:
        if not profile_input:
            response = {"applied": False, "message": "No shipping rate changes to apply"}
        else:
            response = run_update(context, profile["id"], profile_input)

    output(
        {
            "ok": True,
            "mode": "sync-manual-shipping-rates",
            "applied": bool(args.apply),
            "store_domain": context["store_domain"],
            "profile": {"id": profile.get("id"), "name": profile.get("name")},
            "policy_file": str(Path(args.policy_file).resolve()),
            "costs_file": str(Path(args.costs_file).resolve()) if args.costs_file else None,
            "changes": report,
            "mutation_payload": {
                "id": profile.get("id"),
                "profile": profile_input,
            },
            "apply_result": response,
        }
    )


if __name__ == "__main__":
    main()
