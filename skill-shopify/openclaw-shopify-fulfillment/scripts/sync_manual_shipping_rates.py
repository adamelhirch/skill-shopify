import argparse
import json
from pathlib import Path
import re

from shopify_admin_ops import fail, graph_ql, output, resolve_context


DEFAULT_POLICY_FILE = Path(__file__).resolve().parents[1] / "assets" / "manual-rate-policy.json"
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync manual Shopify delivery rates by zone/service using cost + margin rules."
    )
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    parser.add_argument("--profile-id")
    parser.add_argument("--profile-name")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY_FILE))
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


def validate_margin_percent(value: float, label: str) -> float:
    if value < 0.0 or value > 100.0:
        fail(f"{label} must be between 0 and 100")
    return value


def validate_colis_types(colis_types: dict, active_colis_type: str | None) -> tuple[dict, str | None]:
    if not colis_types:
        return {}, active_colis_type
    if not isinstance(colis_types, dict):
        fail("colis_types must be a dictionary in policy-file")
    for colis_type, definition in colis_types.items():
        key = str(colis_type).strip()
        if not key:
            fail("colis_types keys must be non-empty strings")
        if not isinstance(definition, dict):
            fail(f"colis_types['{key}'] must be an object")
    if active_colis_type and active_colis_type not in colis_types:
        fail(f"active_colis_type '{active_colis_type}' is not defined in colis_types")
    return colis_types, active_colis_type


def normalize_id_list(raw_ids, label: str) -> list[str]:
    if raw_ids in (None, ""):
        return []
    if not isinstance(raw_ids, list):
        fail(f"{label} must be a list of Shopify IDs")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        candidate = str(raw_id or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def validate_zone_definitions(profile_policy: dict) -> None:
    zones = profile_policy.get("zones")
    if not isinstance(zones, list) or not zones:
        fail("profile policy must contain a non-empty zones array")

    for zone in zones:
        if not isinstance(zone, dict):
            fail("Each zones entry must be an object")
        zone_name = str(zone.get("zone_name") or "").strip()
        country_codes = normalize_country_codes(zone.get("country_codes"), f"Zone '{zone_name}' country_codes")
        services = zone.get("services")
        if not zone_name:
            fail("Each zone must define zone_name")
        if not isinstance(services, list) or not services:
            fail(f"Zone '{zone_name}' must define a non-empty services list")
        zone["country_codes"] = country_codes
        for service in services:
            if not isinstance(service, dict):
                fail(f"Zone '{zone_name}' has an invalid service entry")
            method_name = str(service.get("method_name") or "").strip()
            if not method_name:
                fail(f"Zone '{zone_name}' has a service missing method_name")

            has_base_cost = "base_cost" in service
            has_tier_cost = "base_cost_by_colis_type" in service
            if has_base_cost and has_tier_cost:
                fail(
                    f"Zone '{zone_name}' / method '{method_name}' must define only one of "
                    "base_cost or base_cost_by_colis_type"
                )
            if not has_base_cost and not has_tier_cost:
                fail(
                    f"Zone '{zone_name}' / method '{method_name}' must define base_cost or "
                    "base_cost_by_colis_type"
                )
            if has_tier_cost:
                by_type = service.get("base_cost_by_colis_type")
                if not isinstance(by_type, dict) or not by_type:
                    fail(
                        f"Zone '{zone_name}' / method '{method_name}' has invalid base_cost_by_colis_type; "
                        "expected non-empty object"
                    )
                active_colis_type = str(profile_policy.get("active_colis_type") or "").strip() or None
                if not active_colis_type:
                    fail(
                        f"Zone '{zone_name}' / method '{method_name}' uses base_cost_by_colis_type, "
                        "but ACTIVE_COLIS_TYPE is not set"
                    )
                if active_colis_type not in by_type:
                    fail(
                        f"Zone '{zone_name}' / method '{method_name}' missing cost for ACTIVE_COLIS_TYPE "
                        f"'{active_colis_type}'"
                    )
            if "margin_percent" in service:
                validate_margin_percent(float(service["margin_percent"]), f"{zone_name}/{method_name} margin_percent")


def validate_profile_policy(
    profile_policy: dict,
    default_delete_unmanaged: bool,
    fallback_profile_name: str | None = None,
    fallback_profile_id: str | None = None,
) -> dict:
    if not isinstance(profile_policy, dict):
        fail("Each profile entry must be an object")

    normalized = dict(profile_policy)
    normalized["currency_code"] = str(normalized.get("currency_code") or "EUR")
    normalized["default_margin_percent"] = validate_margin_percent(
        float(normalized.get("default_margin_percent", 0.0)),
        "default_margin_percent",
    )
    normalized["default_margin_fixed"] = float(normalized.get("default_margin_fixed", 0.0))
    normalized["default_min_price"] = float(normalized.get("default_min_price", 0.0))
    normalized["default_max_price"] = float(normalized.get("default_max_price", 999999.0))
    normalized["delete_unmanaged"] = bool(normalized.get("delete_unmanaged", default_delete_unmanaged))
    colis_types, active_colis_type = validate_colis_types(
        normalized.get("colis_types", {}),
        str(normalized.get("active_colis_type", "")).strip() or None,
    )
    normalized["colis_types"] = colis_types
    normalized["active_colis_type"] = active_colis_type
    normalized["profile_name"] = str(normalized.get("profile_name") or fallback_profile_name or "").strip() or None
    normalized["profile_id"] = str(normalized.get("profile_id") or fallback_profile_id or "").strip() or None
    normalized["create_if_missing"] = bool(normalized.get("create_if_missing", False))
    normalized["location_ids"] = normalize_id_list(
        normalized.get("location_ids"),
        f"profile '{normalized['profile_name'] or normalized['profile_id'] or 'unknown'}' location_ids",
    )
    if not normalized["profile_name"] and not normalized["profile_id"]:
        fail("Each profile policy must define profile_name or profile_id")
    validate_zone_definitions(normalized)
    return normalized


def normalize_country_codes(raw_codes, label: str) -> list[str]:
    if raw_codes in (None, ""):
        return []
    if not isinstance(raw_codes, list):
        fail(f"{label} must be a list of ISO alpha-2 country codes")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_code in raw_codes:
        code = str(raw_code or "").strip().upper()
        if not code:
            continue
        if not COUNTRY_CODE_PATTERN.match(code):
            fail(f"{label} contains invalid country code '{raw_code}'")
        if code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    return normalized


def validate_policy(policy: dict) -> bool:
    if not isinstance(policy, dict):
        fail("policy-file must contain a JSON object")

    currency_code = str(policy.get("currency_code") or "EUR")
    margin_percent = validate_margin_percent(float(policy.get("default_margin_percent", 0.0)), "default_margin_percent")
    margin_fixed = float(policy.get("default_margin_fixed", 0.0))
    min_price = float(policy.get("default_min_price", 0.0))
    max_price = float(policy.get("default_max_price", 999999.0))
    delete_unmanaged_default = bool(policy.get("delete_unmanaged", False))
    colis_types = policy.get("colis_types", {})
    active_colis_type = str(policy.get("active_colis_type", "")).strip() or None
    colis_types, active_colis_type = validate_colis_types(colis_types, active_colis_type)

    policy["currency_code"] = currency_code
    policy["default_margin_percent"] = margin_percent
    policy["default_margin_fixed"] = margin_fixed
    policy["default_min_price"] = min_price
    policy["default_max_price"] = max_price
    policy["colis_types"] = colis_types
    policy["active_colis_type"] = active_colis_type
    profiles = policy.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, list) or not profiles:
            fail("policy-file profiles must be a non-empty array")
        base_profile_defaults = {
            "currency_code": currency_code,
            "default_margin_percent": margin_percent,
            "default_margin_fixed": margin_fixed,
            "default_min_price": min_price,
            "default_max_price": max_price,
            "colis_types": colis_types,
            "active_colis_type": active_colis_type,
            "delete_unmanaged": delete_unmanaged_default,
        }
        normalized_profiles = []
        for profile in profiles:
            merged_profile = dict(base_profile_defaults)
            if not isinstance(profile, dict):
                fail("Each profile entry must be an object")
            merged_profile.update(profile)
            normalized_profiles.append(
                validate_profile_policy(
                    merged_profile,
                    default_delete_unmanaged=delete_unmanaged_default,
                )
            )
        policy["profiles"] = normalized_profiles
    else:
        policy["_normalized_profiles"] = [
            validate_profile_policy(
                policy,
                default_delete_unmanaged=delete_unmanaged_default,
                fallback_profile_name=str(policy.get("profile_name") or "General profile"),
                fallback_profile_id=str(policy.get("profile_id") or "").strip() or None,
            )
        ]
    policy["delete_unmanaged"] = delete_unmanaged_default
    return delete_unmanaged_default


def resolve_policy(args: argparse.Namespace) -> tuple[dict, str, bool]:
    policy = read_json(args.policy_file, "policy-file")
    default_delete_unmanaged = validate_policy(policy)
    source = str(Path(args.policy_file).resolve())
    return policy, source, default_delete_unmanaged


def compute_price(cost: float, margin_percent: float, margin_fixed: float, min_price: float, max_price: float) -> float:
    raw = (cost * (1.0 + margin_percent / 100.0)) + margin_fixed
    clamped = max(min_price, min(max_price, raw))
    return round(clamped + 1e-9, 2)


def resolve_source_cost(service: dict, zone_name: str, active_colis_type: str | None) -> tuple[float, str]:
    by_type = service.get("base_cost_by_colis_type")
    if by_type is not None:
        if not isinstance(by_type, dict) or not by_type:
            fail(
                f"Zone '{zone_name}' / method '{service.get('method_name')}' has invalid "
                "base_cost_by_colis_type"
            )
        if not active_colis_type:
            fail(
                f"Zone '{zone_name}' / method '{service.get('method_name')}' requires ACTIVE_COLIS_TYPE "
                "because base_cost_by_colis_type is used"
            )
        if active_colis_type not in by_type:
            fail(
                f"Zone '{zone_name}' / method '{service.get('method_name')}' has no base cost for "
                f"ACTIVE_COLIS_TYPE '{active_colis_type}'"
            )
        try:
            return float(by_type[active_colis_type]), f"base_cost_by_colis_type[{active_colis_type}]"
        except (TypeError, ValueError):
            fail(
                f"Zone '{zone_name}' / method '{service.get('method_name')}' has invalid numeric cost "
                f"for ACTIVE_COLIS_TYPE '{active_colis_type}'"
            )

    try:
        return float(service["base_cost"]), "base_cost"
    except (KeyError, TypeError, ValueError):
        fail(
            f"Zone '{zone_name}' / method '{service.get('method_name')}' must define numeric base_cost "
            "or base_cost_by_colis_type"
        )


def build_method_definition_input(
    method_name: str,
    description: str | None,
    price: float,
    currency_code: str,
    existing_method_id: str | None = None,
    existing_rate_id: str | None = None,
) -> dict:
    payload = {
        "name": method_name,
        "active": True,
        "rateDefinition": {
            "price": {
                "amount": str(price),
                "currencyCode": currency_code,
            }
        },
    }
    if description:
        payload["description"] = description
    if existing_method_id:
        payload["id"] = existing_method_id
    if existing_rate_id:
        payload["rateDefinition"]["id"] = existing_rate_id
    return payload


def get_profiles(context: dict) -> list[dict]:
    data = graph_ql(
        context,
        """
        query DeliveryProfilesForSync {
          deliveryProfiles(first: 50) {
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


def get_locations(context: dict) -> list[dict]:
    data = graph_ql(
        context,
        """
        query LocationsForShippingSync {
          locations(first: 50) {
            nodes {
              id
              name
              fulfillsOnlineOrders
              isActive
            }
          }
        }
        """,
    )
    return data.get("locations", {}).get("nodes", [])


def lookup_profile(profiles: list[dict], profile_id: str | None, profile_name: str | None) -> dict | None:
    if profile_id:
        for profile in profiles:
            if profile.get("id") == profile_id:
                return profile
        return None
    if profile_name:
        for profile in profiles:
            if profile.get("name") == profile_name:
                return profile
    return None


def find_profile(profiles: list[dict], profile_id: str | None, profile_name: str) -> dict:
    profile = lookup_profile(profiles, profile_id, profile_name)
    if profile:
        return profile
    if profile_id:
        fail(f"Profile not found by id: {profile_id}")
    fail(f"Profile not found by name: {profile_name}")


def resolve_target_profiles(policy: dict, args: argparse.Namespace) -> list[dict]:
    profiles = policy.get("profiles") or policy.get("_normalized_profiles") or []
    if not profiles:
        fail("No profile policies were resolved from policy-file")

    if args.profile_id:
        selected = [profile for profile in profiles if profile.get("profile_id") == args.profile_id]
        if selected:
            return selected
        if len(profiles) == 1:
            overridden = dict(profiles[0])
            overridden["profile_id"] = args.profile_id
            return [overridden]
        fail(f"No profile policy matched --profile-id {args.profile_id}")

    if args.profile_name:
        selected = [profile for profile in profiles if profile.get("profile_name") == args.profile_name]
        if selected:
            return selected
        if len(profiles) == 1:
            overridden = dict(profiles[0])
            overridden["profile_name"] = args.profile_name
            return [overridden]
        fail(f"No profile policy matched --profile-name {args.profile_name}")

    return profiles


def resolve_location_ids(context: dict, profile_policy: dict) -> list[str]:
    configured = profile_policy.get("location_ids") or []
    if configured:
        return configured

    active_locations = [
        location.get("id")
        for location in get_locations(context)
        if location.get("id") and location.get("isActive") and location.get("fulfillsOnlineOrders")
    ]
    if not active_locations:
        fail(
            "No active fulfillment locations were found. Provide location_ids in the profile policy or activate a Shopify location."
        )
    return active_locations


def build_zone_create_payload(zone_policy: dict, policy: dict) -> tuple[dict | None, dict]:
    currency_code = str(policy.get("currency_code") or "EUR")
    default_margin_percent = float(policy.get("default_margin_percent", 0.0))
    default_margin_fixed = float(policy.get("default_margin_fixed", 0.0))
    default_min_price = float(policy.get("default_min_price", 0.0))
    default_max_price = float(policy.get("default_max_price", 999999.0))
    active_colis_type = str(policy.get("active_colis_type") or "").strip() or None

    zone_name = str(zone_policy.get("zone_name") or "").strip()
    country_codes = normalize_country_codes(
        zone_policy.get("country_codes"),
        f"Zone '{zone_name}' country_codes",
    )
    services = zone_policy.get("services")
    if not zone_name or not isinstance(services, list):
        return None, {"zone_name": zone_name, "status": "skipped"}

    methods_to_create = []
    service_report = []
    for service in services:
        if not isinstance(service, dict):
            continue
        if service.get("active", True) is False:
            continue
        method_name = str(service.get("method_name") or "").strip()
        if not method_name:
            continue
        description = str(service.get("description") or "").strip() or None
        source_cost, source_cost_mode = resolve_source_cost(
            service=service,
            zone_name=zone_name,
            active_colis_type=active_colis_type,
        )
        margin_percent = float(service.get("margin_percent", default_margin_percent))
        margin_fixed = float(service.get("margin_fixed", default_margin_fixed))
        min_price = float(service.get("min_price", default_min_price))
        max_price = float(service.get("max_price", default_max_price))
        price = compute_price(source_cost, margin_percent, margin_fixed, min_price, max_price)
        carrier_name = str(service.get("carrier_name") or "").strip() or None
        carrier_service_name = str(service.get("carrier_service_name") or "").strip() or None
        carrier_service_code = str(service.get("carrier_service_code") or "").strip() or None
        methods_to_create.append(
            build_method_definition_input(
                method_name=method_name,
                description=description,
                price=price,
                currency_code=currency_code,
            )
        )
        service_report.append(
            {
                "method_name": method_name,
                "description": description,
                "carrier_name": carrier_name,
                "carrier_service_name": carrier_service_name,
                "carrier_service_code": carrier_service_code,
                "status": "create",
                "colis_type": active_colis_type,
                "source_cost_mode": source_cost_mode,
                "source_cost": source_cost,
                "target_price": price,
            }
        )

    if not methods_to_create:
        return None, {
            "zone_name": zone_name,
            "status": "zone_missing",
            "message": "Zone not found in delivery profile and no active services were defined",
        }

    return (
        {
            "name": zone_name,
            "countries": [{"code": code, "includeAllProvinces": True} for code in country_codes],
            "methodDefinitionsToCreate": methods_to_create,
        },
        {
            "zone_name": zone_name,
            "status": "zone_create",
            "country_codes": country_codes,
            "methods": service_report,
        },
    )


def build_zone_updates(
    profile: dict,
    policy: dict,
    delete_unmanaged: bool,
) -> tuple[list[dict], list[dict], list[str]]:
    currency_code = str(policy.get("currency_code") or "EUR")
    default_margin_percent = float(policy.get("default_margin_percent", 0.0))
    default_margin_fixed = float(policy.get("default_margin_fixed", 0.0))
    default_min_price = float(policy.get("default_min_price", 0.0))
    default_max_price = float(policy.get("default_max_price", 999999.0))
    active_colis_type = str(policy.get("active_colis_type") or "").strip() or None
    zone_policies = policy.get("zones")
    if not isinstance(zone_policies, list) or not zone_policies:
        fail("policy-file must contain a non-empty zones array")

    report = []
    location_group_updates = []
    method_ids_to_delete: list[str] = []

    for group in profile.get("profileLocationGroups", []):
        group_id = group.get("locationGroup", {}).get("id")
        if not group_id:
            continue
        zone_updates = []
        zone_creates = []
        zones = group.get("locationGroupZones", {}).get("nodes", [])
        zones_by_name = {
            (zone_node.get("zone", {}).get("name") or ""): zone_node
            for zone_node in zones
        }

        for zone_policy in zone_policies:
            if not isinstance(zone_policy, dict):
                continue
            zone_name = str(zone_policy.get("zone_name") or "").strip()
            country_codes = normalize_country_codes(
                zone_policy.get("country_codes"),
                f"Zone '{zone_name}' country_codes",
            )
            services = zone_policy.get("services")
            if not zone_name or not isinstance(services, list):
                continue
            zone_node = zones_by_name.get(zone_name)
            if not zone_node:
                if not country_codes:
                    report.append(
                        {
                            "zone_name": zone_name,
                            "status": "zone_missing",
                            "message": "Zone not found in delivery profile and country_codes is missing",
                        }
                    )
                    continue

                zone_create, zone_report = build_zone_create_payload(zone_policy, policy)
                if not zone_create:
                    report.append(zone_report)
                    continue

                zone_creates.append(zone_create)
                report.append(zone_report)
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
                if not method_name:
                    continue
                description = str(service.get("description") or "").strip() or None
                managed_names.add(method_name)
                source_cost, source_cost_mode = resolve_source_cost(
                    service=service,
                    zone_name=zone_name,
                    active_colis_type=active_colis_type,
                )

                margin_percent = float(service.get("margin_percent", default_margin_percent))
                margin_fixed = float(service.get("margin_fixed", default_margin_fixed))
                min_price = float(service.get("min_price", default_min_price))
                max_price = float(service.get("max_price", default_max_price))
                price = compute_price(source_cost, margin_percent, margin_fixed, min_price, max_price)

                carrier_name = str(service.get("carrier_name") or "").strip() or None
                carrier_service_name = str(service.get("carrier_service_name") or "").strip() or None
                carrier_service_code = str(service.get("carrier_service_code") or "").strip() or None

                existing = existing_by_name.get(method_name)
                if existing:
                    rate_provider = existing.get("rateProvider") or {}
                    rate_id = rate_provider.get("id")
                    current_price_raw = (rate_provider.get("price") or {}).get("amount")
                    current_price = float(current_price_raw) if current_price_raw is not None else None
                    update_payload = build_method_definition_input(
                        method_name=method_name,
                        description=description,
                        price=price,
                        currency_code=currency_code,
                        existing_method_id=existing.get("id"),
                        existing_rate_id=rate_id,
                    )
                    methods_to_update.append(update_payload)
                    service_report.append(
                        {
                            "method_name": method_name,
                            "description": description,
                            "carrier_name": carrier_name,
                            "carrier_service_name": carrier_service_name,
                            "carrier_service_code": carrier_service_code,
                            "status": "update",
                            "colis_type": active_colis_type,
                            "source_cost_mode": source_cost_mode,
                            "source_cost": source_cost,
                            "target_price": price,
                            "current_price": current_price,
                        }
                    )
                else:
                    methods_to_create.append(
                        build_method_definition_input(
                            method_name=method_name,
                            description=description,
                            price=price,
                            currency_code=currency_code,
                        )
                    )
                    service_report.append(
                        {
                            "method_name": method_name,
                            "description": description,
                            "carrier_name": carrier_name,
                            "carrier_service_name": carrier_service_name,
                            "carrier_service_code": carrier_service_code,
                            "status": "create",
                            "colis_type": active_colis_type,
                            "source_cost_mode": source_cost_mode,
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
                    method_ids_to_delete.append(method.get("id"))
                    service_report.append(
                        {
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
            group_update = {"id": group_id}
            if zone_creates:
                group_update["zonesToCreate"] = zone_creates
            if zone_updates:
                group_update["zonesToUpdate"] = zone_updates
            location_group_updates.append(group_update)
        elif zone_creates:
            location_group_updates.append({"id": group_id, "zonesToCreate": zone_creates})

    return location_group_updates, report, method_ids_to_delete


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


def build_profile_create_input(context: dict, profile_policy: dict) -> tuple[dict, list[dict]]:
    location_ids = resolve_location_ids(context, profile_policy)
    zone_creates: list[dict] = []
    report: list[dict] = []
    for zone_policy in profile_policy.get("zones", []):
        if not isinstance(zone_policy, dict):
            continue
        zone_create, zone_report = build_zone_create_payload(zone_policy, profile_policy)
        report.append(zone_report)
        if zone_create:
            zone_creates.append(zone_create)
    if not zone_creates:
        fail(
            f"Profile '{profile_policy.get('profile_name') or profile_policy.get('profile_id')}' has no active zones to create"
        )
    return {
        "name": profile_policy.get("profile_name"),
        "locationGroupsToCreate": [
            {
                "locations": location_ids,
                "zonesToCreate": zone_creates,
            }
        ],
    }, report


def run_create(context: dict, profile_input: dict) -> dict:
    return graph_ql(
        context,
        """
        mutation CreateDeliveryProfile($profile: DeliveryProfileInput!) {
          deliveryProfileCreate(profile: $profile) {
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
        {"profile": profile_input},
    )


def main() -> None:
    args = parse_args()
    context = resolve_context(args)
    policy, policy_source, default_delete_unmanaged = resolve_policy(args)
    existing_profiles = get_profiles(context)
    target_profiles = resolve_target_profiles(policy, args)
    results = []

    for target_policy in target_profiles:
        effective_delete_unmanaged = bool(args.delete_unmanaged or target_policy.get("delete_unmanaged", default_delete_unmanaged))
        existing_profile = lookup_profile(
            existing_profiles,
            target_policy.get("profile_id"),
            target_policy.get("profile_name"),
        )

        if existing_profile:
            location_group_updates, report, method_ids_to_delete = build_zone_updates(
                profile=existing_profile,
                policy=target_policy,
                delete_unmanaged=effective_delete_unmanaged,
            )
            profile_input = {}
            if location_group_updates:
                profile_input["locationGroupsToUpdate"] = location_group_updates
            if method_ids_to_delete:
                profile_input["methodDefinitionsToDelete"] = method_ids_to_delete

            response = None
            if args.apply:
                if not profile_input:
                    response = {"applied": False, "message": "No shipping rate changes to apply"}
                else:
                    response = run_update(context, existing_profile["id"], profile_input)

            results.append(
                {
                    "operation": "update",
                    "profile": {"id": existing_profile.get("id"), "name": existing_profile.get("name")},
                    "active_colis_type": target_policy.get("active_colis_type"),
                    "delete_unmanaged": effective_delete_unmanaged,
                    "changes": report,
                    "mutation_payload": {
                        "id": existing_profile.get("id"),
                        "profile": profile_input,
                    },
                    "apply_result": response,
                }
            )
            continue

        if not target_policy.get("create_if_missing"):
            results.append(
                {
                    "operation": "missing",
                    "profile": {"id": target_policy.get("profile_id"), "name": target_policy.get("profile_name")},
                    "active_colis_type": target_policy.get("active_colis_type"),
                    "delete_unmanaged": effective_delete_unmanaged,
                    "changes": [],
                    "mutation_payload": None,
                    "apply_result": {
                        "applied": False,
                        "message": "Profile not found and create_if_missing is false",
                    },
                }
            )
            continue

        profile_input, report = build_profile_create_input(context, target_policy)
        response = None
        if args.apply:
            response = run_create(context, profile_input)

        results.append(
            {
                "operation": "create",
                "profile": {"id": None, "name": target_policy.get("profile_name")},
                "active_colis_type": target_policy.get("active_colis_type"),
                "delete_unmanaged": effective_delete_unmanaged,
                "changes": report,
                "mutation_payload": {
                    "profile": profile_input,
                },
                "apply_result": response,
            }
        )

    payload = {
        "ok": True,
        "mode": "sync-manual-shipping-rates",
        "applied": bool(args.apply),
        "store_domain": context["store_domain"],
        "policy_source": policy_source,
        "profiles": results,
    }
    if len(results) == 1:
        single = results[0]
        payload.update(
            {
                "profile": single["profile"],
                "active_colis_type": single["active_colis_type"],
                "delete_unmanaged": single["delete_unmanaged"],
                "changes": single["changes"],
                "mutation_payload": single["mutation_payload"],
                "apply_result": single["apply_result"],
                "operation": single["operation"],
            }
        )
    output(payload)


if __name__ == "__main__":
    main()
