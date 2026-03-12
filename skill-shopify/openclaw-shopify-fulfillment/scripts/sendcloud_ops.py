import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from shopify_admin_ops import fail, graph_ql, output, resolve_context, resolve_order_id


DEFAULT_PACKAGES_FILE = Path(__file__).resolve().parents[1] / "assets" / "package-catalog.sendcloud.json"
DEFAULT_ROUTING_POLICY_FILE = Path(__file__).resolve().parents[1] / "assets" / "sendcloud-routing-policy.json"
DEFAULT_SENDCLOUD_BASE_URL = "https://panel.sendcloud.sc"
DEFAULT_SENDCLOUD_TOKEN_URL = "https://account.sendcloud.com/oauth2/token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sendcloud API helper for shipping methods and parcel labels.")
    sub = parser.add_subparsers(dest="command", required=True)

    context_parser = sub.add_parser("context", help="Validate Sendcloud credentials by reading user context.")
    add_sendcloud_auth_args(context_parser)

    methods_parser = sub.add_parser("shipping-methods-list", help="List available Sendcloud shipping methods.")
    add_sendcloud_auth_args(methods_parser)
    methods_parser.add_argument("--from-country")
    methods_parser.add_argument("--to-country")
    methods_parser.add_argument("--from-postal-code")
    methods_parser.add_argument("--to-postal-code")
    methods_parser.add_argument("--weight")

    parcel_parser = sub.add_parser("parcel-create", help="Create a Sendcloud parcel directly from JSON payload.")
    add_sendcloud_auth_args(parcel_parser)
    parcel_parser.add_argument("--parcel-json")
    parcel_parser.add_argument("--parcel-file")
    parcel_parser.add_argument("--dry-run", action="store_true")

    order_parser = sub.add_parser(
        "parcel-create-from-order",
        help="Create a Sendcloud parcel from a Shopify order using package catalog and logistics metafields.",
    )
    add_sendcloud_auth_args(order_parser)
    add_shopify_context_args(order_parser)
    order_parser.add_argument("--order-id")
    order_parser.add_argument("--order-name")
    order_parser.add_argument("--packages-file", default=str(DEFAULT_PACKAGES_FILE))
    order_parser.add_argument("--routing-policy-file", default=str(DEFAULT_ROUTING_POLICY_FILE))
    order_parser.add_argument("--shipping-method-id")
    order_parser.add_argument("--sender-address-id")
    order_parser.add_argument("--allow-oversize-package", action="store_true")
    order_parser.add_argument("--request-label", action="store_true", default=True)
    order_parser.add_argument("--no-request-label", action="store_true")
    order_parser.add_argument("--apply-shipping-rules", action="store_true", default=True)
    order_parser.add_argument("--no-apply-shipping-rules", action="store_true")
    order_parser.add_argument("--extra-parcel-json")
    order_parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def add_sendcloud_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sendcloud-public-key")
    parser.add_argument("--sendcloud-secret-key")
    parser.add_argument("--sendcloud-api-base-url")
    parser.add_argument("--sendcloud-token-url")
    parser.add_argument("--sendcloud-auth-mode", choices=["auto", "basic", "oauth2"])


def add_shopify_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")


def env_value(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def parse_json_arg(raw: str | None, label: str) -> dict:
    if not raw:
        fail(f"Missing {label}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON for {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value


def parse_price(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_routing_policy(path: str) -> tuple[dict, str]:
    policy_path = Path(path)
    if not policy_path.exists():
        fail(f"Routing policy file not found: {policy_path}")
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"Unable to read routing policy file: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON in routing policy file: {exc}")
    if not isinstance(payload, dict):
        fail("routing-policy-file must contain a JSON object")
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        fail("routing-policy-file must contain a non-empty rules array")
    return payload, str(policy_path.resolve())


def effective_method_price(method: dict) -> float | None:
    top_level_price = parse_price(method.get("price"))
    country_prices = []
    for country in method.get("countries") or []:
        candidate = parse_price(country.get("price"))
        if candidate is not None:
            country_prices.append(candidate)
    positive_prices = [price for price in country_prices if price > 0]
    if positive_prices:
        return min(positive_prices)
    if top_level_price is not None and top_level_price > 0:
        return top_level_price
    return None


def method_matches_family(method: dict, family: dict, defaults: dict) -> bool:
    carrier = str(method.get("carrier") or "").strip().casefold()
    name = str(method.get("name") or "").strip().casefold()
    service_point_input = str(method.get("service_point_input") or "").strip().casefold()

    expected_carrier = str(family.get("carrier") or "").strip().casefold()
    if expected_carrier and carrier != expected_carrier:
        return False

    expected_spi = str(family.get("service_point_input") or "").strip().casefold()
    if expected_spi and service_point_input != expected_spi:
        return False

    contains = [str(value).strip().casefold() for value in family.get("name_contains_any") or [] if str(value).strip()]
    if contains and not any(fragment in name for fragment in contains):
        return False

    excludes = [str(value).strip().casefold() for value in family.get("name_excludes_any") or [] if str(value).strip()]
    if excludes and any(fragment in name for fragment in excludes):
        return False

    price = effective_method_price(method)
    require_positive_price = bool(defaults.get("require_positive_price", True))
    if require_positive_price and (price is None or price <= 0):
        return False
    maximum_price = parse_price(defaults.get("maximum_price"))
    if maximum_price is not None and price is not None and price > maximum_price:
        return False
    return True


def choose_method_from_rule(methods: list[dict], rule: dict, defaults: dict) -> dict | None:
    strategy = str(rule.get("selection_strategy") or "priority").strip().lower()
    families = rule.get("families") or []
    if not isinstance(families, list) or not families:
        return None

    family_candidates = []
    for family in families:
        if not isinstance(family, dict):
            continue
        candidates = [method for method in methods if method_matches_family(method, family, defaults)]
        if not candidates:
            continue
        candidates.sort(key=lambda method: (effective_method_price(method) or 999999.0, int(method.get("id") or 0)))
        family_candidates.append({"family": family, "method": candidates[0]})

    if not family_candidates:
        return None
    if strategy == "cheapest":
        family_candidates.sort(key=lambda entry: (effective_method_price(entry["method"]) or 999999.0, int(entry["method"].get("id") or 0)))
        return family_candidates[0]["method"]
    return family_candidates[0]["method"]


def find_routing_rule(policy: dict, country_code: str, checkout_method_name: str) -> dict | None:
    normalized_country = str(country_code or "").strip().upper()
    normalized_method = str(checkout_method_name or "").strip().casefold()
    for rule in policy.get("rules") or []:
        countries = {str(code).strip().upper() for code in rule.get("country_codes") or [] if str(code).strip()}
        method_names = {str(name).strip().casefold() for name in rule.get("checkout_method_names") or [] if str(name).strip()}
        if normalized_country not in countries:
            continue
        if normalized_method not in method_names:
            continue
        return rule
    return None


def select_routed_shipping_method(
    client: "SendcloudClient",
    routing_policy: dict,
    order: dict,
    package_choice: dict,
) -> tuple[dict, dict, dict]:
    shipping_lines = (order.get("shippingLines") or {}).get("nodes") or []
    shipping_line = shipping_lines[0] if shipping_lines else None
    if not shipping_line:
        fail("Order has no shipping line; cannot resolve Sendcloud routing policy")

    recipient = order.get("shippingAddress") or {}
    country_code = str(recipient.get("countryCodeV2") or "").strip().upper()
    postal_code = str(recipient.get("zip") or "").strip()
    if not country_code or not postal_code:
        fail("Order shipping address is missing countryCodeV2 or zip; cannot resolve Sendcloud routing policy")

    method_title = str(shipping_line.get("title") or "").strip()
    routing_rule = find_routing_rule(routing_policy, country_code, method_title)
    if not routing_rule:
        fail(f"No Sendcloud routing rule matched country={country_code} method='{method_title}'")

    query = {
        "from_country": "FR",
        "to_country": country_code,
        "from_postal_code": env_value("SENDCLOUD_FROM_POSTAL_CODE") or env_value("CHRONOPOST_SHIPPER_POSTAL_CODE") or "34550",
        "to_postal_code": postal_code,
        "weight": f"{float(package_choice.get('shipment_weight_kg') or 0.0):.3f}",
    }
    response = client.request("GET", "/api/v2/shipping_methods", query=query)
    methods = response.get("shipping_methods")
    if not isinstance(methods, list):
        fail("Unexpected Sendcloud shipping methods response shape while resolving routing")

    selected = choose_method_from_rule(methods, routing_rule, routing_policy.get("selection_defaults") or {})
    if not selected:
        fail(f"No compatible Sendcloud shipping method matched country={country_code} method='{method_title}'")
    return selected, routing_rule, shipping_line


class SendcloudClient:
    def __init__(
        self,
        public_key: str,
        secret_key: str,
        api_base_url: str,
        token_url: str,
        auth_mode: str,
    ) -> None:
        self.public_key = public_key
        self.secret_key = secret_key
        self.api_base_url = api_base_url.rstrip("/")
        self.token_url = token_url
        self.auth_mode = auth_mode
        self._oauth_token: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, str] | None = None,
        auth_mode: str = "basic",
    ) -> dict:
        url = f"{self.api_base_url}{path}"
        if query:
            cleaned = {key: str(value) for key, value in query.items() if value is not None and str(value).strip()}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned)}"

        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None

        if auth_mode == "oauth2":
            headers["Authorization"] = f"Bearer {self._get_oauth_token()}"
        else:
            auth = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {auth}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} on {path}: {body}") from exc
        except OSError as exc:
            raise RuntimeError(f"Connection error on {path}: {exc}") from exc

        try:
            payload_out = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response on {path}: {exc}") from exc
        if not isinstance(payload_out, dict):
            raise RuntimeError(f"Unexpected Sendcloud response shape on {path}")
        return payload_out

    def _get_oauth_token(self) -> str:
        if self._oauth_token:
            return self._oauth_token
        auth = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            self.token_url,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8"),
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OAuth2 token error {exc.code}: {body}") from exc
        except OSError as exc:
            raise RuntimeError(f"OAuth2 token connection error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid OAuth2 token response JSON: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("OAuth2 token response missing access_token")
        self._oauth_token = str(token)
        return self._oauth_token

    def request(self, method: str, path: str, payload: dict | None = None, query: dict[str, str] | None = None) -> dict:
        if self.auth_mode in {"basic", "oauth2"}:
            return self._request(method, path, payload=payload, query=query, auth_mode=self.auth_mode)

        last_error = None
        for mode in ("basic", "oauth2"):
            try:
                return self._request(method, path, payload=payload, query=query, auth_mode=mode)
            except RuntimeError as exc:
                last_error = exc
        fail(f"Sendcloud request failed in both auth modes: {last_error}")


def resolve_sendcloud_client(args: argparse.Namespace) -> SendcloudClient:
    public_key = args.sendcloud_public_key or env_value("SENDCLOUD_PUBLIC_KEY")
    secret_key = args.sendcloud_secret_key or env_value("SENDCLOUD_SECRET_KEY")
    api_base_url = (
        args.sendcloud_api_base_url
        or env_value("SENDCLOUD_API_BASE_URL")
        or DEFAULT_SENDCLOUD_BASE_URL
    )
    token_url = args.sendcloud_token_url or env_value("SENDCLOUD_TOKEN_URL") or DEFAULT_SENDCLOUD_TOKEN_URL
    auth_mode = args.sendcloud_auth_mode or env_value("SENDCLOUD_AUTH_MODE") or "auto"
    if not public_key or not secret_key:
        fail("Missing Sendcloud credentials: SENDCLOUD_PUBLIC_KEY and SENDCLOUD_SECRET_KEY")
    return SendcloudClient(public_key, secret_key, api_base_url, token_url, auth_mode)


def load_packages(path: str) -> tuple[list[dict], str]:
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
    return valid, str(Path(path).resolve())


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


def choose_oversize_fallback_package(packages: list[dict], total_item_weight_kg: float, aggregate_dims: list[float]) -> dict:
    ranked = []
    for package in packages:
        try:
            package_dims = normalized_dims(
                float(package["inner_length_cm"]),
                float(package["inner_width_cm"]),
                float(package["inner_height_cm"]),
            )
            empty_weight = float(package["empty_weight_kg"])
            max_weight = float(package["max_weight_kg"])
            volume = package_dims[0] * package_dims[1] * package_dims[2]
        except (KeyError, TypeError, ValueError):
            continue
        overweight = max(0.0, (total_item_weight_kg + empty_weight) - max_weight)
        dims_gap = sum(max(0.0, aggregate_dims[index] - package_dims[index]) for index in range(3))
        ranked.append((overweight, dims_gap, volume, package, package_dims, empty_weight))
    if not ranked:
        fail("No valid enabled package available for oversize fallback")
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    _, _, _, package, package_dims, empty_weight = ranked[0]
    return {
        "package": package,
        "package_dims_sorted_cm": package_dims,
        "aggregate_item_dims_sorted_cm": aggregate_dims,
        "shipment_weight_kg": round(total_item_weight_kg + empty_weight, 3),
        "oversize_fallback": True,
    }


def decimal_metafield(variant: dict, key: str) -> float | None:
    for metafield in variant.get("metafields", {}).get("nodes", []):
        if metafield.get("key") == key:
            try:
                return float(metafield.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def get_order(context: dict, order_gid: str) -> dict:
    data = graph_ql(
        context,
        """
        query SendcloudParcelOrder($id: ID!) {
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
              provinceCode
              zip
              country
              countryCodeV2
              phone
            }
            shippingLines(first: 20) {
              nodes {
                title
                code
                carrierIdentifier
              }
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
        query SendcloudParcelVariants($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on ProductVariant {
              id
              sku
              inventoryItem {
                measurement {
                  weight {
                    value
                  }
                }
              }
              metafields(first: 20, namespace: "openclaw_logistics") {
                nodes {
                  key
                  value
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


def split_address(address_line: str) -> tuple[str, str | None]:
    raw = (address_line or "").strip()
    if not raw:
        return "", None
    match = re.match(r"^\s*(\d+[A-Za-z]?)\s+(.*)$", raw)
    if match:
        return match.group(2).strip(), match.group(1).strip()
    return raw, None


def build_parcel_payload_from_order(
    order: dict,
    package_choice: dict,
    planned_items: list[dict],
    shipping_method_id: str | None,
    sender_address_id: str | None,
    request_label: bool,
    apply_shipping_rules: bool,
    extra_parcel_fields: dict | None,
) -> dict:
    recipient = order.get("shippingAddress") or {}
    first_name = str(recipient.get("firstName") or "").strip()
    last_name = str(recipient.get("lastName") or "").strip()
    company_name = str(recipient.get("company") or "").strip()
    full_name = f"{first_name} {last_name}".strip() or company_name or "Client"
    address1 = str(recipient.get("address1") or "").strip()
    address2 = str(recipient.get("address2") or "").strip()
    street, house_number = split_address(address1)
    package = package_choice.get("package") or {}

    parcel = {
        "name": full_name,
        "company_name": company_name or None,
        "address": street or address1 or "Address required",
        "house_number": house_number,
        "address_2": address2 or None,
        "city": str(recipient.get("city") or "").strip(),
        "postal_code": str(recipient.get("zip") or "").strip(),
        "country": str(recipient.get("countryCodeV2") or "").strip() or str(recipient.get("country") or "").strip(),
        "state": str(recipient.get("provinceCode") or "").strip() or None,
        "telephone": str(recipient.get("phone") or "").strip() or None,
        "email": str(order.get("email") or "").strip() or None,
        "weight": f"{float(package_choice.get('shipment_weight_kg') or 0.0):.3f}",
        "length": f"{float(package.get('inner_length_cm') or 0.0):.1f}",
        "width": f"{float(package.get('inner_width_cm') or 0.0):.1f}",
        "height": f"{float(package.get('inner_height_cm') or 0.0):.1f}",
        "order_number": str(order.get("name") or "").strip(),
        "external_reference": str(order.get("id") or "").strip(),
        "request_label": bool(request_label),
        "apply_shipping_rules": bool(apply_shipping_rules),
        "total_order_value_currency": "EUR",
    }

    if shipping_method_id:
        parcel["shipment"] = int(shipping_method_id)
    if sender_address_id:
        parcel["sender_address"] = int(sender_address_id)

    parcel_items = []
    for item in planned_items:
        parcel_items.append(
            {
                "description": item.get("title"),
                "quantity": int(item.get("quantity") or 1),
                "sku": item.get("sku") or None,
                "weight": f"{float(item.get('shipping_weight_kg') or 0.0):.3f}",
            }
        )
    if parcel_items:
        parcel["parcel_items"] = parcel_items

    if extra_parcel_fields:
        parcel.update(extra_parcel_fields)

    clean = {}
    for key, value in parcel.items():
        if value is None:
            continue
        clean[key] = value
    return {"parcel": clean}


def build_order_plan(
    context: dict,
    order_gid: str,
    packages_file: str,
    allow_oversize_package: bool,
) -> tuple[dict, list[dict], list[dict], dict, str]:
    packages, package_source = load_packages(packages_file)
    order = get_order(context, order_gid)
    line_items = (order.get("lineItems") or {}).get("nodes") or []
    variant_ids = [line["variant"]["id"] for line in line_items if line.get("variant") and line["variant"].get("id")]
    variants = get_variants(context, variant_ids)

    planned_items: list[dict] = []
    missing: list[dict] = []
    item_dims: list[list[float]] = []
    total_item_weight_kg = 0.0

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
                "shipping_weight_kg": shipping_weight_kg,
                "dimensions_cm": {"length": length_cm, "width": width_cm, "height": height_cm},
            }
        )

    if missing:
        fail("Order is missing logistics fields for Sendcloud parcel creation: " + json.dumps(missing, ensure_ascii=True))
    package_choice = choose_package(packages, item_dims, total_item_weight_kg)
    if not package_choice:
        if not allow_oversize_package:
            fail("No enabled package can fit this order with current package catalog")
        aggregate_dims = [
            max(dims[0] for dims in item_dims),
            max(dims[1] for dims in item_dims),
            sum(dims[2] for dims in item_dims),
        ]
        package_choice = choose_oversize_fallback_package(packages, total_item_weight_kg, aggregate_dims)
    return order, planned_items, missing, package_choice, package_source


def command_context(args: argparse.Namespace) -> None:
    client = resolve_sendcloud_client(args)
    data = client.request("GET", "/api/v2/user")
    output(
        {
            "ok": True,
            "mode": "sendcloud-context",
            "auth_mode": client.auth_mode,
            "api_base_url": client.api_base_url,
            "data": data,
        }
    )


def command_shipping_methods(args: argparse.Namespace) -> None:
    client = resolve_sendcloud_client(args)
    query = {
        "from_country": args.from_country,
        "to_country": args.to_country,
        "from_postal_code": args.from_postal_code,
        "to_postal_code": args.to_postal_code,
        "weight": args.weight,
    }
    data = client.request("GET", "/api/v2/shipping_methods", query=query)
    methods = data.get("shipping_methods")
    output(
        {
            "ok": True,
            "mode": "sendcloud-shipping-methods-list",
            "count": len(methods) if isinstance(methods, list) else None,
            "data": data,
        }
    )


def command_parcel_create(args: argparse.Namespace) -> None:
    payload: dict | None = None
    if args.parcel_json:
        payload = parse_json_arg(args.parcel_json, "parcel-json")
    elif args.parcel_file:
        try:
            payload = json.loads(Path(args.parcel_file).read_text(encoding="utf-8"))
        except OSError as exc:
            fail(f"Unable to read parcel-file: {exc}")
        except json.JSONDecodeError as exc:
            fail(f"Invalid JSON in parcel-file: {exc}")
        if not isinstance(payload, dict):
            fail("parcel-file must contain a JSON object")
    else:
        fail("Provide --parcel-json or --parcel-file")

    if "parcel" not in payload:
        payload = {"parcel": payload}

    if args.dry_run:
        output({"ok": True, "mode": "sendcloud-parcel-create", "dry_run": True, "payload": payload})
        return

    client = resolve_sendcloud_client(args)
    data = client.request("POST", "/api/v2/parcels", payload=payload)
    output({"ok": True, "mode": "sendcloud-parcel-create", "dry_run": False, "data": data})


def command_parcel_create_from_order(args: argparse.Namespace) -> None:
    shopify_context = resolve_context(args)
    order_gid = resolve_order_id(shopify_context, args.order_id, args.order_name)
    order, planned_items, _, package_choice, package_source = build_order_plan(
        context=shopify_context,
        order_gid=order_gid,
        packages_file=args.packages_file,
        allow_oversize_package=bool(args.allow_oversize_package),
    )

    extra_parcel_fields = parse_json_arg(args.extra_parcel_json, "extra-parcel-json") if args.extra_parcel_json else None
    request_label = False if args.no_request_label else bool(args.request_label)
    apply_shipping_rules = False if args.no_apply_shipping_rules else bool(args.apply_shipping_rules)
    resolved_shipping_method = None
    matched_routing_rule = None
    routing_policy_source = None

    shipping_method_id = args.shipping_method_id
    if not shipping_method_id:
        routing_policy, routing_policy_source = load_routing_policy(args.routing_policy_file)
        sendcloud_client = resolve_sendcloud_client(args)
        resolved_shipping_method, matched_routing_rule, _ = select_routed_shipping_method(
            client=sendcloud_client,
            routing_policy=routing_policy,
            order=order,
            package_choice=package_choice,
        )
        shipping_method_id = str(resolved_shipping_method["id"])
        apply_shipping_rules = False

    payload = build_parcel_payload_from_order(
        order=order,
        package_choice=package_choice,
        planned_items=planned_items,
        shipping_method_id=shipping_method_id,
        sender_address_id=args.sender_address_id,
        request_label=request_label,
        apply_shipping_rules=apply_shipping_rules,
        extra_parcel_fields=extra_parcel_fields,
    )
    warnings = []
    if package_choice.get("oversize_fallback"):
        warnings.append("No package fit found; used oversize fallback package due to --allow-oversize-package.")

    if args.dry_run:
        output(
            {
                "ok": True,
                "mode": "sendcloud-parcel-create-from-order",
                "dry_run": True,
                "store_domain": shopify_context["store_domain"],
                "order": {"id": order.get("id"), "name": order.get("name")},
                "packages_source": package_source,
                "package_choice": package_choice,
                "planned_items": planned_items,
                "warnings": warnings,
                "routing_policy_source": routing_policy_source,
                "matched_routing_rule": matched_routing_rule,
                "resolved_shipping_method": resolved_shipping_method,
                "payload": payload,
            }
        )
        return

    sendcloud_client = resolve_sendcloud_client(args)
    data = sendcloud_client.request("POST", "/api/v2/parcels", payload=payload)
    output(
        {
            "ok": True,
            "mode": "sendcloud-parcel-create-from-order",
            "dry_run": False,
            "store_domain": shopify_context["store_domain"],
            "order": {"id": order.get("id"), "name": order.get("name")},
            "packages_source": package_source,
            "package_choice": package_choice,
            "planned_items": planned_items,
            "warnings": warnings,
            "routing_policy_source": routing_policy_source,
            "matched_routing_rule": matched_routing_rule,
            "resolved_shipping_method": resolved_shipping_method,
            "payload": payload,
            "data": data,
        }
    )


def main() -> None:
    args = parse_args()
    if args.command == "context":
        command_context(args)
        return
    if args.command == "shipping-methods-list":
        command_shipping_methods(args)
        return
    if args.command == "parcel-create":
        command_parcel_create(args)
        return
    if args.command == "parcel-create-from-order":
        command_parcel_create_from_order(args)
        return
    fail(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
