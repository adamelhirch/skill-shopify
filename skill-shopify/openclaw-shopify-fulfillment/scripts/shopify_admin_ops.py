import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_API_VERSION = "2026-01"
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


def load_openclaw_env() -> dict[str, str]:
    candidates = [
        Path.cwd() / "openclaw.json",
        Path(__file__).resolve().parents[4] / "openclaw.json",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vars_map = ((raw.get("env") or {}).get("vars") or {})
        if isinstance(vars_map, dict):
            return {key: str(value) for key, value in vars_map.items() if value is not None}
    return {}


OPENCLAW_ENV = load_openclaw_env()


def env_value(name: str) -> str | None:
    return os.environ.get(name) or OPENCLAW_ENV.get(name)


def fail(message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=True))
    raise SystemExit(code)


def output(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def normalize_shop_domain(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" in candidate:
        parsed = urllib.parse.urlparse(candidate)
        candidate = parsed.netloc or parsed.path
    candidate = candidate.strip().strip("/")
    return candidate.replace("https://", "").replace("http://", "")


def parse_json_value(raw: str | None, expected: type | tuple[type, ...], label: str):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON for {label}: {exc}")
    if not isinstance(value, expected):
        fail(f"{label} must be JSON {expected}")
    return value


def load_token_cache() -> dict[str, dict]:
    if not TOKEN_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


TOKEN_CACHE_PATH = Path(tempfile.gettempdir()) / "openclaw-shopify-client-token-cache.json"
TOKEN_REFRESH_SKEW_SECONDS = 60


def save_token_cache(payload: dict[str, dict]) -> None:
    try:
        TOKEN_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        return


def token_cache_key(store_domain: str, client_id: str) -> str:
    return f"{store_domain}|{client_id}"


def request_client_credentials_token(store_domain: str, client_id: str, client_secret: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://{store_domain}/admin/oauth/access_token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"Shopify token HTTP error {exc.code}: {body}")
    except OSError as exc:
        fail(f"Shopify token connection error: {exc}")

    access_token = data.get("access_token")
    if not access_token:
        fail("Shopify token response did not include access_token")

    expires_in_raw = data.get("expires_in")
    try:
        expires_in = int(expires_in_raw) if expires_in_raw is not None else 86400
    except (TypeError, ValueError):
        expires_in = 86400

    now = time.time()
    return {
        "access_token": access_token,
        "granted_scope": data.get("scope"),
        "expires_at": now + max(expires_in, 60),
        "issued_at": now,
    }


def resolve_access_token(
    store_domain: str,
    explicit_token: str | None,
    client_id: str | None,
    client_secret: str | None,
) -> dict:
    if explicit_token:
        return {
            "access_token": explicit_token,
            "token_source": "explicit-override",
            "granted_scope": None,
            "expires_at": None,
        }

    missing = [
        name
        for name, value in [
            ("SHOPIFY_CLIENT_ID", client_id),
            ("SHOPIFY_CLIENT_SECRET", client_secret),
        ]
        if not value
    ]
    if missing:
        fail("Missing Shopify context: " + ", ".join(missing))

    cache = load_token_cache()
    cache_key = token_cache_key(store_domain, str(client_id))
    cached = cache.get(cache_key)
    now = time.time()
    if isinstance(cached, dict):
        cached_token = cached.get("access_token")
        cached_expires_at = cached.get("expires_at")
        try:
            cached_expires_at_value = float(cached_expires_at) if cached_expires_at is not None else 0.0
        except (TypeError, ValueError):
            cached_expires_at_value = 0.0
        if cached_token and cached_expires_at_value > now + TOKEN_REFRESH_SKEW_SECONDS:
            return {
                "access_token": str(cached_token),
                "token_source": "client-credentials-cache",
                "granted_scope": cached.get("granted_scope"),
                "expires_at": cached_expires_at_value,
            }

    fresh = request_client_credentials_token(store_domain, str(client_id), str(client_secret))
    cache[cache_key] = fresh
    save_token_cache(cache)
    return {
        "access_token": fresh["access_token"],
        "token_source": "client-credentials-grant",
        "granted_scope": fresh.get("granted_scope"),
        "expires_at": fresh.get("expires_at"),
    }


def resolve_context(args: argparse.Namespace) -> dict:
    legacy_shop_url = args.shop_url or env_value("SHOPIFY_SHOP_URL")
    store_domain = normalize_shop_domain(args.store or env_value("SHOPIFY_STORE_DOMAIN") or legacy_shop_url)
    client_id = args.client_id or env_value("SHOPIFY_CLIENT_ID")
    client_secret = args.client_secret or env_value("SHOPIFY_CLIENT_SECRET")
    requested_scope = args.scope or env_value("SHOPIFY_SCOPE")
    api_version = args.api_version or env_value("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    explicit_token = args.token or env_value("SHOPIFY_ACCESS_TOKEN")

    if not store_domain:
        fail("Missing Shopify context: SHOPIFY_STORE_DOMAIN")

    token_data = resolve_access_token(store_domain, explicit_token, client_id, client_secret)
    effective_scope = token_data.get("granted_scope") or requested_scope

    return {
        "store_domain": store_domain,
        "access_token": token_data["access_token"],
        "client_id": client_id,
        "client_secret": client_secret,
        "requested_scope": requested_scope,
        "scope": effective_scope,
        "token_source": token_data.get("token_source"),
        "token_expires_at": token_data.get("expires_at"),
        "api_version": api_version,
        "shop_url": legacy_shop_url,
    }


def granted_scopes(context: dict) -> set[str]:
    raw_scope = context.get("scope") or ""
    return {entry.strip() for entry in raw_scope.split(",") if entry.strip()}


def has_scope(context: dict, scope_name: str) -> bool:
    return scope_name in granted_scopes(context)


def graph_ql(context: dict, query: str, variables: dict | None = None) -> dict:
    result = graph_ql_allow_errors(context, query, variables)
    if result.get("errors"):
        fail("Shopify GraphQL errors: " + json.dumps(result["errors"], ensure_ascii=True))
    return result.get("data") or {}


def graph_ql_allow_errors(context: dict, query: str, variables: dict | None = None) -> dict:
    url = f"https://{context['store_domain']}/admin/api/{context['api_version']}/graphql.json"
    payload = json.dumps({"query": query, "variables": variables or {}}, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": context["access_token"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"Shopify HTTP error {exc.code}: {body}")
    except OSError as exc:
        fail(f"Shopify connection error: {exc}")
    return {
        "data": data.get("data") or {},
        "errors": data.get("errors") or [],
    }


def graphql_operation(args: argparse.Namespace, query: str, variables: dict | None = None) -> None:
    context = resolve_context(args)
    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": args.command,
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "query": query,
                "variables": variables or {},
            }
        )
        return
    data = graph_ql(context, query, variables)
    output({"ok": True, "mode": args.command, "shop": context["store_domain"], "api_version": context["api_version"], "data": data})


def as_gid(resource: str, value: str) -> str:
    if value.startswith("gid://shopify/"):
        return value
    return f"gid://shopify/{resource}/{value}"


def read_query_argument(args: argparse.Namespace) -> str:
    if getattr(args, "query_file", None):
        with open(args.query_file, "r", encoding="utf-8") as handle:
            return handle.read()
    if getattr(args, "query", None):
        return args.query
    fail("Missing query text or query file")


def first_node(nodes: list[dict], label: str) -> dict:
    if not nodes:
        fail(f"No {label} found")
    return nodes[0]


def require_scopes(context: dict, *scope_names: str) -> None:
    if not context.get("scope"):
        return
    missing = [scope_name for scope_name in scope_names if not has_scope(context, scope_name)]
    if missing:
        fail("Missing required Shopify scopes: " + ", ".join(missing))


def require_any_scope(context: dict, *scope_names: str) -> None:
    if not context.get("scope"):
        return
    if any(has_scope(context, scope_name) for scope_name in scope_names):
        return
    fail("Missing required Shopify scope. Expected one of: " + ", ".join(scope_names))


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_country_codes(raw_values: list[str] | None) -> list[str]:
    if not raw_values:
        fail("Provide at least one --country-code")
    normalized: list[str] = []
    for raw_value in raw_values:
        for part in raw_value.split(","):
            candidate = part.strip().upper()
            if not candidate:
                continue
            if not COUNTRY_CODE_PATTERN.match(candidate):
                fail(f"Invalid country code: {part.strip() or raw_value}")
            normalized.append(candidate)
    normalized = unique_preserve_order(normalized)
    if not normalized:
        fail("Provide at least one --country-code")
    return normalized


def serialize_market_region(region: dict) -> dict:
    payload = {
        "type": region.get("__typename"),
        "name": region.get("name"),
    }
    if region.get("__typename") == "MarketRegionCountry":
        payload["id"] = region.get("id")
        payload["code"] = region.get("code")
    return payload


def serialize_market_record(record: dict) -> dict:
    regions = [serialize_market_region(region) for region in ((record.get("regions") or {}).get("nodes") or [])]
    country_codes = [
        region["code"]
        for region in regions
        if region.get("type") == "MarketRegionCountry" and region.get("code")
    ]
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "regions": regions,
        "country_codes": country_codes,
    }


def fetch_market_by_id(context: dict, market_gid: str) -> dict:
    query = """
    query MarketGet($id: ID!) {
      market(id: $id) {
        id
        name
        regions(first: 250) {
          nodes {
            __typename
            name
            ... on MarketRegionCountry {
              id
              code
            }
          }
        }
      }
    }
    """
    data = graph_ql(context, query, {"id": market_gid})
    market = data.get("market")
    if not market:
        fail(f"No market found for id {market_gid}")
    return market


def find_market_by_name(context: dict, market_name: str) -> dict:
    query = """
    query MarketsLookup($first: Int!) {
      markets(first: $first) {
        nodes {
          id
          name
          regions(first: 250) {
            nodes {
              __typename
              name
              ... on MarketRegionCountry {
                id
                code
              }
            }
          }
        }
      }
    }
    """
    data = graph_ql(context, query, {"first": 100})
    normalized_target = market_name.strip().casefold()
    candidates = [
        market
        for market in data.get("markets", {}).get("nodes", [])
        if str(market.get("name") or "").strip().casefold() == normalized_target
    ]
    if not candidates:
        fail(f"No market found for name {market_name}")
    if len(candidates) > 1:
        fail(f"Multiple markets matched name {market_name}; use --market-id")
    return candidates[0]


def resolve_market_id(context: dict, market_id: str | None = None, market_name: str | None = None) -> str:
    if market_id:
        return as_gid("Market", market_id)
    if market_name:
        return find_market_by_name(context, market_name)["id"]
    fail("Provide --market-id or --market-name")


def resolve_order_id(context: dict, order_id: str | None = None, order_name: str | None = None) -> str:
    if order_id:
        return as_gid("Order", order_id)
    if not order_name:
        fail("Provide --order-id or --order-name")
    query = """
    query OrderByName($first: Int!, $query: String!) {
      orders(first: $first, query: $query, reverse: true) {
        nodes {
          id
          name
        }
      }
    }
    """
    data = graph_ql(context, query, {"first": 5, "query": f"name:{order_name}"})
    record = first_node(data["orders"]["nodes"], "order")
    return record["id"]


def resolve_product_id(context: dict, product_id: str | None = None, handle: str | None = None) -> str:
    if product_id:
        return as_gid("Product", product_id)
    if not handle:
        fail("Provide --product-id or --handle")
    query = """
    query ProductByHandle($query: String!) {
      products(first: 1, query: $query) {
        nodes {
          id
          title
          handle
        }
      }
    }
    """
    data = graph_ql(context, query, {"query": f"handle:{handle}"})
    record = first_node(data["products"]["nodes"], "product")
    return record["id"]


def resolve_variant_id(
    context: dict,
    variant_id: str | None = None,
    sku: str | None = None,
    handle: str | None = None,
) -> str:
    if variant_id:
        return as_gid("ProductVariant", variant_id)
    if sku:
        query = """
        query VariantBySku($query: String!) {
          productVariants(first: 5, query: $query) {
            nodes {
              id
              title
              sku
              product {
                title
                handle
              }
            }
          }
        }
        """
        data = graph_ql(context, query, {"query": f"sku:{sku}"})
        record = first_node(data["productVariants"]["nodes"], "variant")
        return record["id"]
    product_gid = resolve_product_id(context, handle=handle)
    query = """
    query FirstVariantByProduct($id: ID!) {
      product(id: $id) {
        id
        title
        variants(first: 1) {
          nodes {
            id
            title
          }
        }
      }
    }
    """
    data = graph_ql(context, query, {"id": product_gid})
    record = first_node(data["product"]["variants"]["nodes"], "variant")
    return record["id"]


def list_delivery_profiles(context: dict, first: int = 50) -> list[dict]:
    data = graph_ql(
        context,
        """
        query DeliveryProfilesList($first: Int!) {
          deliveryProfiles(first: $first) {
            nodes {
              id
              name
            }
          }
        }
        """,
        {"first": first},
    )
    return data.get("deliveryProfiles", {}).get("nodes", [])


def resolve_delivery_profile(context: dict, profile_id: str | None = None, profile_name: str | None = None) -> dict:
    if not profile_id and not profile_name:
        fail("Provide --profile-id or --profile-name")
    target_id = as_gid("DeliveryProfile", profile_id) if profile_id else None
    target_name = str(profile_name or "").strip().casefold() or None
    candidates = []
    for profile in list_delivery_profiles(context):
        current_id = profile.get("id")
        current_name = str(profile.get("name") or "").strip()
        if target_id and current_id == target_id:
            candidates.append(profile)
        elif target_name and current_name.casefold() == target_name:
            candidates.append(profile)
    if not candidates:
        fail(f"No delivery profile found for {profile_id or profile_name}")
    if len(candidates) > 1:
        fail(f"Multiple delivery profiles matched {profile_id or profile_name}; use --profile-id")
    return candidates[0]


def list_shippable_variants(context: dict, page_size: int = 100, query_filter: str | None = None) -> list[dict]:
    after_cursor = None
    variants: list[dict] = []
    while True:
        data = graph_ql(
            context,
            """
            query ShippableVariants($first: Int!, $after: String, $query: String) {
              productVariants(first: $first, after: $after, query: $query) {
                nodes {
                  id
                  sku
                  title
                  deliveryProfile {
                    id
                    name
                  }
                  inventoryItem {
                    id
                    tracked
                    requiresShipping
                  }
                  product {
                    id
                    title
                    handle
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
            {"first": page_size, "after": after_cursor, "query": query_filter},
        )
        page = data.get("productVariants", {})
        for variant in page.get("nodes", []):
            inventory_item = variant.get("inventoryItem") or {}
            if inventory_item.get("requiresShipping") is not True:
                continue
            variants.append(variant)
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after_cursor = page_info.get("endCursor")
        if not after_cursor:
            break
    return variants


def fetch_variants_by_ids(context: dict, variant_ids: list[str]) -> list[dict]:
    if not variant_ids:
        return []
    data = graph_ql(
        context,
        """
        query VariantsByIds($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on ProductVariant {
              id
              sku
              title
              deliveryProfile {
                id
                name
              }
              inventoryItem {
                id
                tracked
                requiresShipping
              }
              product {
                id
                title
                handle
                status
              }
            }
          }
        }
        """,
        {"ids": variant_ids},
    )
    return [node for node in (data.get("nodes") or []) if node]


def update_market_country_codes(context: dict, market_gid: str, country_codes: list[str], add: bool) -> dict:
    conditions_key = "conditionsToAdd" if add else "conditionsToDelete"
    mutation = """
    mutation MarketUpdateCountries($id: ID!, $input: MarketUpdateInput!) {
      marketUpdate(id: $id, input: $input) {
        market {
          id
          name
          regions(first: 250) {
            nodes {
              __typename
              name
              ... on MarketRegionCountry {
                id
                code
              }
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
    variables = {
        "id": market_gid,
        "input": {
            "conditions": {
                conditions_key: {
                    "regionsCondition": {
                        "regions": [{"countryCode": code} for code in country_codes],
                    }
                }
            }
        },
    }
    response = graph_ql_allow_errors(context, mutation, variables)
    payload = (response.get("data") or {}).get("marketUpdate") or {}
    user_errors = payload.get("userErrors") or []
    for error in response.get("errors") or []:
        user_errors.append(
            {
                "field": None,
                "message": str(error.get("message") or error),
            }
        )
    return {
        "market": serialize_market_record(payload.get("market") or {}),
        "user_errors": user_errors,
        "query": mutation,
        "variables": variables,
    }


def resolve_customer_id(context: dict, customer_id: str | None = None, email: str | None = None) -> str:
    if customer_id:
        return as_gid("Customer", customer_id)
    if not email:
        fail("Provide --customer-id or --email")
    query = """
    query CustomerByEmail($query: String!) {
      customers(first: 1, query: $query) {
        nodes {
          id
          displayName
          email
        }
      }
    }
    """
    data = graph_ql(context, query, {"query": f"email:{email}"})
    record = first_node(data["customers"]["nodes"], "customer")
    return record["id"]


def command_context(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    output(
        {
            "ok": True,
            "mode": "context",
            "context": {
                "store_domain": context["store_domain"],
                "api_version": context["api_version"],
                "has_client_id": bool(context["client_id"]),
                "has_client_secret": bool(context["client_secret"]),
                "has_webhook_secret": bool(env_value("SHOPIFY_WEBHOOK_SECRET") or env_value("SHOPIFY_CLIENT_SECRET")),
                "access_token_source": context.get("token_source"),
                "token_expires_at": context.get("token_expires_at"),
                "requested_scope": context.get("requested_scope"),
                "granted_scope": context.get("scope"),
            },
        }
    )


def command_ping(args: argparse.Namespace) -> None:
    graphql_operation(args, "query { shop { name myshopifyDomain plan { displayName } } }")


def command_shop_info(args: argparse.Namespace) -> None:
    graphql_operation(
        args,
        """
        query {
          shop {
            name
            myshopifyDomain
            email
            contactEmail
            currencyCode
            plan {
              displayName
            }
            shipsToCountries
          }
        }
        """,
    )


def command_orders_list(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    customer_block = """
              customer {
                displayName
                email
              }
""" if has_scope(context, "read_customers") else ""
    query = """
        query OrdersList($first: Int!, $query: String) {
          orders(first: $first, reverse: true, query: $query) {
            nodes {
              id
              name
              createdAt
              displayFinancialStatus
              displayFulfillmentStatus
              canMarkAsPaid
              closed
              cancelledAt
              currentTotalPriceSet {
                shopMoney {
                  amount
                  currencyCode
                }
              }
__CUSTOMER_BLOCK__              tags
              note
            }
          }
        }
    """.replace("__CUSTOMER_BLOCK__", customer_block)
    if getattr(args, "dry_run", False):
        output({
            "ok": True,
            "mode": args.command,
            "dry_run": True,
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "query": query,
            "variables": {"first": args.first, "query": args.query},
        })
        return
    data = graph_ql(context, query, {"first": args.first, "query": args.query})
    output({"ok": True, "mode": args.command, "shop": context["store_domain"], "api_version": context["api_version"], "data": data})


def command_order_get(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    customer_block = """
        customer {
          id
          displayName
          email
        }
""" if has_scope(context, "read_customers") else ""
    query = """
    query OrderGet($id: ID!) {
      order(id: $id) {
        id
        name
        createdAt
        displayFinancialStatus
        displayFulfillmentStatus
        canMarkAsPaid
        closed
        cancelledAt
        note
        tags
        email
__CUSTOMER_BLOCK__        shippingAddress {
          address1
          address2
          city
          province
          zip
          country
          phone
          firstName
          lastName
          company
        }
        lineItems(first: 50) {
          nodes {
            id
            name
            quantity
            sku
            originalUnitPriceSet {
              shopMoney {
                amount
                currencyCode
              }
            }
          }
        }
        fulfillments {
          id
          status
          trackingInfo {
            company
            number
            url
          }
        }
      }
    }
    """.replace("__CUSTOMER_BLOCK__", customer_block)
    data = graph_ql(context, query, {"id": order_gid})
    output({"ok": True, "mode": "order-get", "data": data})


def command_order_update(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    shipping_address = parse_json_value(args.shipping_address_json, dict, "shipping_address_json") if args.shipping_address_json else None
    input_payload = {"id": order_gid}
    if args.note is not None:
        input_payload["note"] = args.note
    if args.email is not None:
        input_payload["email"] = args.email
    if args.tags:
        input_payload["tags"] = args.tags
    if shipping_address:
        input_payload["shippingAddress"] = shipping_address
    if len(input_payload) == 1:
        fail("Provide at least one mutable field: --note, --email, --tags, or --shipping-address-json")
    graphql_operation(
        args,
        """
        mutation OrderUpdate($input: OrderInput!) {
          orderUpdate(input: $input) {
            order {
              id
              name
              note
              email
              tags
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": input_payload},
    )


def command_order_mark_paid(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    graphql_operation(
        args,
        """
        mutation OrderMarkAsPaid($input: OrderMarkAsPaidInput!) {
          orderMarkAsPaid(input: $input) {
            order {
              id
              name
              canMarkAsPaid
              displayFinancialStatus
              totalOutstandingSet {
                shopMoney {
                  amount
                  currencyCode
                }
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": {"id": order_gid}},
    )


def command_fulfillment_orders_for_order(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    graphql_operation(
        args,
        """
        query FulfillmentOrdersForOrder($id: ID!) {
          order(id: $id) {
            id
            name
            fulfillmentOrders(first: 50) {
              nodes {
                id
                status
                requestStatus
                supportedActions {
                  action
                }
                assignedLocation {
                  name
                  location {
                    id
                    name
                  }
                }
                lineItems(first: 50) {
                  nodes {
                    id
                    remainingQuantity
                    totalQuantity
                    lineItem {
                      name
                      sku
                    }
                  }
                }
              }
            }
          }
        }
        """,
        {"id": order_gid},
    )


def command_fulfillment_create(args: argparse.Namespace) -> None:
    fulfillment_input = parse_json_value(args.input_json, dict, "input_json")
    graphql_operation(
        args,
        """
        mutation FulfillmentCreate($fulfillment: FulfillmentInput!, $message: String) {
          fulfillmentCreate(fulfillment: $fulfillment, message: $message) {
            fulfillment {
              id
              status
              trackingInfo(first: 10) {
                company
                number
                url
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"fulfillment": fulfillment_input, "message": args.message},
    )


def command_products_search(args: argparse.Namespace) -> None:
    graphql_operation(
        args,
        """
        query ProductsSearch($first: Int!, $query: String) {
          products(first: $first, reverse: true, query: $query) {
            nodes {
              id
              title
              handle
              status
              vendor
              productType
              tags
              totalInventory
            }
          }
        }
        """,
        {"first": args.first, "query": args.query},
    )


def command_products_by_sku(args: argparse.Namespace) -> None:
    query_filter = " OR ".join(f"sku:{sku}" for sku in args.sku)
    graphql_operation(
        args,
        """
        query ProductVariantsBySku($query: String!, $first: Int!) {
          productVariants(first: $first, query: $query) {
            nodes {
              id
              title
              sku
              inventoryQuantity
              inventoryItem {
                id
              }
              product {
                id
                title
                handle
                status
                vendor
              }
            }
          }
        }
        """,
        {"query": query_filter, "first": max(args.first, len(args.sku))},
    )


def command_product_get(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    if args.sku:
        query_filter = f"sku:{args.sku}"
        data = graph_ql(
            context,
            """
            query ProductBySku($query: String!) {
              productVariants(first: 5, query: $query) {
                nodes {
                  id
                  title
                  sku
                  inventoryQuantity
                  product {
                    id
                    title
                    handle
                    status
                    vendor
                    productType
                    tags
                    totalInventory
                  }
                }
              }
            }
            """,
            {"query": query_filter},
        )
        output({"ok": True, "mode": "product-get", "data": data})
        return
    product_gid = resolve_product_id(context, args.product_id, args.handle)
    data = graph_ql(
        context,
        """
        query ProductGet($id: ID!) {
          product(id: $id) {
            id
            title
            descriptionHtml
            handle
            status
            vendor
            productType
            tags
            totalInventory
            variants(first: 50) {
              nodes {
                id
                title
                sku
                inventoryQuantity
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
                metafields(first: 20, namespace: "openclaw_logistics") {
                  nodes {
                    id
                    namespace
                    key
                    type
                    value
                  }
                }
              }
            }
          }
        }
        """,
        {"id": product_gid},
    )
    output({"ok": True, "mode": "product-get", "data": data})


def command_product_update(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    if args.input_json:
        product_input = parse_json_value(args.input_json, dict, "input_json")
    else:
        product_gid = resolve_product_id(context, args.product_id, args.handle)
        product_input = {"id": product_gid}
        if args.title is not None:
            product_input["title"] = args.title
        if args.status is not None:
            product_input["status"] = args.status
        if args.vendor is not None:
            product_input["vendor"] = args.vendor
        if args.product_type is not None:
            product_input["productType"] = args.product_type
        if args.tags:
            product_input["tags"] = args.tags
        if len(product_input) == 1:
            fail("Provide --input-json or at least one field to update")
    graphql_operation(
        args,
        """
        mutation ProductUpdate($product: ProductUpdateInput!) {
          productUpdate(product: $product) {
            product {
              id
              title
              handle
              status
              vendor
              productType
              tags
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"product": product_input},
    )


def command_delivery_profiles_list(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    profiles = list_delivery_profiles(context, first=args.first)
    output(
        {
            "ok": True,
            "mode": "delivery-profiles-list",
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "profiles": profiles,
        }
    )


def command_variants_shippable_list(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    target_profile = None
    if args.profile_id or args.profile_name:
        target_profile = resolve_delivery_profile(context, args.profile_id, args.profile_name)
    variants = list_shippable_variants(context, page_size=args.first, query_filter=args.query)
    if target_profile:
        target_id = target_profile["id"]
        matching = [variant for variant in variants if ((variant.get("deliveryProfile") or {}).get("id") == target_id)]
        mismatched = [variant for variant in variants if ((variant.get("deliveryProfile") or {}).get("id") != target_id)]
    else:
        matching = variants
        mismatched = []

    selected = matching if args.only_matching else mismatched if args.only_mismatched else variants
    if args.limit is not None:
        selected = selected[: args.limit]
    output(
        {
            "ok": True,
            "mode": "variants-shippable-list",
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "profile": target_profile,
            "counts": {
                "shippable": len(variants),
                "matching_profile": len(matching),
                "mismatched_profile": len(mismatched),
                "selected": len(selected),
            },
            "variants": selected,
        }
    )


def command_delivery_profile_assign_variants(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    target_profile = resolve_delivery_profile(context, args.profile_id, args.profile_name)
    if args.variant_id:
        variant_ids = unique_preserve_order([as_gid("ProductVariant", value) for value in args.variant_id])
        variants = fetch_variants_by_ids(context, variant_ids)
    else:
        variants = list_shippable_variants(context, page_size=args.first, query_filter=args.query)

    target_profile_id = target_profile["id"]
    skipped_non_shippable = []
    selected_variants = []
    for variant in variants:
        inventory_item = variant.get("inventoryItem") or {}
        if inventory_item.get("requiresShipping") is not True:
            skipped_non_shippable.append(variant.get("id"))
            continue
        current_profile_id = (variant.get("deliveryProfile") or {}).get("id")
        if current_profile_id == target_profile_id:
            continue
        selected_variants.append(variant)

    if args.limit is not None:
        selected_variants = selected_variants[: args.limit]

    batch_size = max(1, min(args.batch_size, 200))
    batches = [
        [variant["id"] for variant in selected_variants[index : index + batch_size]]
        for index in range(0, len(selected_variants), batch_size)
    ]

    mutation = """
    mutation AssignVariantsToDeliveryProfile($id: ID!, $profile: DeliveryProfileInput!) {
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
    """
    preview_batches = [{"id": target_profile_id, "profile": {"variantsToAssociate": batch}} for batch in batches]
    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": "delivery-profile-assign-variants",
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "profile": target_profile,
                "counts": {
                    "scanned": len(variants),
                    "skipped_non_shippable": len(skipped_non_shippable),
                    "already_assigned": len(variants) - len(skipped_non_shippable) - len(selected_variants),
                    "selected": len(selected_variants),
                    "batch_count": len(batches),
                },
                "selected_variants": selected_variants,
                "mutation": mutation,
                "preview_batches": preview_batches,
            }
        )
        return

    apply_results = []
    for batch in batches:
        payload = graph_ql(
            context,
            mutation,
            {
                "id": target_profile_id,
                "profile": {"variantsToAssociate": batch},
            },
        ).get("deliveryProfileUpdate") or {}
        apply_results.append(
            {
                "batch_size": len(batch),
                "user_errors": payload.get("userErrors") or [],
            }
        )
    output(
        {
            "ok": True,
            "mode": "delivery-profile-assign-variants",
            "dry_run": False,
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "profile": target_profile,
            "counts": {
                "scanned": len(variants),
                "skipped_non_shippable": len(skipped_non_shippable),
                "selected": len(selected_variants),
                "batch_count": len(batches),
            },
            "apply_results": apply_results,
        }
    )


def command_inventory_by_sku(args: argparse.Namespace) -> None:
    query_filter = " OR ".join(f"sku:{sku}" for sku in args.sku)
    graphql_operation(
        args,
        """
        query InventoryBySku($query: String!, $first: Int!) {
          productVariants(first: $first, query: $query) {
            nodes {
              id
              sku
              inventoryQuantity
              inventoryItem {
                id
                tracked
              }
              product {
                title
                handle
                status
              }
            }
          }
        }
        """,
        {"query": query_filter, "first": max(args.first, len(args.sku))},
    )


def command_inventory_adjust(args: argparse.Namespace) -> None:
    input_payload = parse_json_value(args.input_json, dict, "input_json")
    graphql_operation(
        args,
        """
        mutation InventoryAdjustQuantities($input: InventoryAdjustQuantitiesInput!) {
          inventoryAdjustQuantities(input: $input) {
            inventoryAdjustmentGroup {
              createdAt
              reason
              referenceDocumentUri
              changes {
                name
                delta
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": input_payload},
    )


def command_variant_logistics_get(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    variant_gid = resolve_variant_id(context, args.variant_id, args.sku, args.handle)
    graphql_operation(
        args,
        """
        query VariantLogisticsGet($id: ID!) {
          productVariant(id: $id) {
            id
            title
            sku
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
            }
            metafields(first: 20, namespace: "openclaw_logistics") {
              nodes {
                id
                namespace
                key
                type
                value
              }
            }
          }
        }
        """,
        {"id": variant_gid},
    )


def command_variant_logistics_set(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    variant_gid = resolve_variant_id(context, args.variant_id, args.sku, args.handle)
    variant_data = graph_ql(
        context,
        """
        query VariantLogisticsContext($id: ID!) {
          productVariant(id: $id) {
            id
            title
            inventoryItem {
              id
            }
          }
        }
        """,
        {"id": variant_gid},
    )
    variant = variant_data.get("productVariant")
    if not variant:
        fail("Variant not found")

    if (
        args.weight_kg is None
        and args.net_weight_kg is None
        and args.length_cm is None
        and args.width_cm is None
        and args.height_cm is None
        and args.packaging_type is None
    ):
        fail("Provide at least one field to update")

    if args.dry_run:
        payload = {
            "ok": True,
            "mode": args.command,
            "dry_run": True,
            "variant_id": variant_gid,
            "inventory_item_id": variant["inventoryItem"]["id"],
            "updates": {
                "weight_kg": args.weight_kg,
                "net_weight_kg": args.net_weight_kg,
                "length_cm": args.length_cm,
                "width_cm": args.width_cm,
                "height_cm": args.height_cm,
                "packaging_type": args.packaging_type,
            },
        }
        output(payload)
        return

    inventory_result = None
    if args.weight_kg is not None:
        inventory_result = graph_ql(
            context,
            """
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
            """,
            {
                "id": variant["inventoryItem"]["id"],
                "input": {
                    "measurement": {
                        "weight": {
                            "value": args.weight_kg,
                            "unit": "KILOGRAMS",
                        }
                    }
                },
            },
        )

    metafields = []
    logistic_fields = [
        ("packaging_type", "single_line_text_field", args.packaging_type),
        ("net_weight_kg", "number_decimal", args.net_weight_kg),
        ("parcel_length_cm", "number_decimal", args.length_cm),
        ("parcel_width_cm", "number_decimal", args.width_cm),
        ("parcel_height_cm", "number_decimal", args.height_cm),
    ]
    for key, type_name, value in logistic_fields:
        if value is None:
            continue
        metafields.append(
            {
                "ownerId": variant_gid,
                "namespace": "openclaw_logistics",
                "key": key,
                "type": type_name,
                "value": str(value),
            }
        )

    metafields_result = None
    if metafields:
        metafields_result = graph_ql(
            context,
            """
            mutation VariantLogisticsMetafieldsSet($metafields: [MetafieldsSetInput!]!) {
              metafieldsSet(metafields: $metafields) {
                metafields {
                  id
                  namespace
                  key
                  type
                  value
                }
                userErrors {
                  field
                  message
                  code
                }
              }
            }
            """,
            {"metafields": metafields},
        )

    refreshed = graph_ql(
        context,
        """
        query VariantLogisticsGet($id: ID!) {
          productVariant(id: $id) {
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
                type
                value
              }
            }
          }
        }
        """,
        {"id": variant_gid},
    )
    output(
        {
            "ok": True,
            "mode": args.command,
            "variant_id": variant_gid,
            "inventory_item_id": variant["inventoryItem"]["id"],
            "inventory_result": inventory_result,
            "metafields_result": metafields_result,
            "data": refreshed,
        }
    )


def command_customer_get(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    customer_gid = resolve_customer_id(context, args.customer_id, args.email) if (args.customer_id or args.email) else None
    if customer_gid:
        data = graph_ql(
            context,
            """
            query CustomerGet($id: ID!) {
              customer(id: $id) {
                id
                displayName
                email
                phone
                tags
                defaultAddress {
                  address1
                  address2
                  city
                  zip
                  country
                }
              }
            }
            """,
            {"id": customer_gid},
        )
        output({"ok": True, "mode": "customer-get", "data": data})
        return
    graphql_operation(
        args,
        """
        query CustomersSearch($first: Int!, $query: String) {
          customers(first: $first, query: $query) {
            nodes {
              id
              displayName
              email
              phone
              tags
            }
          }
        }
        """,
        {"first": args.first, "query": args.query},
    )


def command_customer_update(args: argparse.Namespace) -> None:
    input_payload = parse_json_value(args.input_json, dict, "input_json")
    graphql_operation(
        args,
        """
        mutation CustomerUpdate($input: CustomerInput!) {
          customerUpdate(input: $input) {
            customer {
              id
              displayName
              email
              phone
              tags
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"input": input_payload},
    )


def command_markets_list(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    require_any_scope(context, "read_markets", "write_markets")
    query = """
    query MarketsList($first: Int!) {
      markets(first: $first) {
        nodes {
          id
          name
          regions(first: 250) {
            nodes {
              __typename
              name
              ... on MarketRegionCountry {
                id
                code
              }
            }
          }
        }
      }
    }
    """
    variables = {"first": args.first}
    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": args.command,
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "query": query,
                "variables": variables,
            }
        )
        return
    data = graph_ql(context, query, variables)
    markets = [serialize_market_record(record) for record in data.get("markets", {}).get("nodes", [])]
    output({"ok": True, "mode": args.command, "shop": context["store_domain"], "api_version": context["api_version"], "markets": markets})


def command_market_get(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    require_any_scope(context, "read_markets", "write_markets")
    market_gid = resolve_market_id(context, args.market_id, args.market_name)
    market = serialize_market_record(fetch_market_by_id(context, market_gid))
    output({"ok": True, "mode": args.command, "shop": context["store_domain"], "api_version": context["api_version"], "market": market})


def command_market_countries_update(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    require_scopes(context, "write_markets")
    market_gid = resolve_market_id(context, args.market_id, args.market_name)
    market_before = serialize_market_record(fetch_market_by_id(context, market_gid))
    country_codes = normalize_country_codes(args.country_code)
    conditions_key = "conditionsToAdd" if args.command == "market-countries-add" else "conditionsToDelete"
    mutation = """
    mutation MarketUpdateCountries($id: ID!, $input: MarketUpdateInput!) {
      marketUpdate(id: $id, input: $input) {
        market {
          id
          name
          regions(first: 250) {
            nodes {
              __typename
              name
              ... on MarketRegionCountry {
                id
                code
              }
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
    variables = {
        "id": market_gid,
        "input": {
            "conditions": {
                conditions_key: {
                    "regionsCondition": {
                        "regions": [{"countryCode": code} for code in country_codes],
                    }
                }
            }
        },
    }
    preview = {
        "market_id": market_gid,
        "market_name": market_before["name"],
        "country_codes": country_codes,
        "action": "add" if args.command == "market-countries-add" else "remove",
        "before_country_codes": market_before["country_codes"],
    }
    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": args.command,
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "preview": preview,
                "query": mutation,
                "variables": variables,
            }
        )
        return
    data = graph_ql(context, mutation, variables)
    payload = data.get("marketUpdate") or {}
    market_after = serialize_market_record(payload.get("market") or {})
    output(
        {
            "ok": True,
            "mode": args.command,
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "preview": preview,
            "market": market_after,
            "user_errors": payload.get("userErrors") or [],
        }
    )


def command_market_countries_ensure(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    require_scopes(context, "write_markets")
    market_gid = resolve_market_id(context, args.market_id, args.market_name)
    market_before = serialize_market_record(fetch_market_by_id(context, market_gid))
    requested = normalize_country_codes(args.country_code)
    already_present = [code for code in requested if code in market_before["country_codes"]]
    missing = [code for code in requested if code not in market_before["country_codes"]]

    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": args.command,
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "market": market_before,
                "already_present": already_present,
                "pending": missing,
            }
        )
        return

    added = []
    skipped = []
    for code in missing:
        result = update_market_country_codes(context, market_gid, [code], add=True)
        errors = result["user_errors"]
        if errors:
            skipped.append({"country_code": code, "user_errors": errors})
            continue
        added.append(code)

    market_after = serialize_market_record(fetch_market_by_id(context, market_gid))
    output(
        {
            "ok": True,
            "mode": args.command,
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "market_before": market_before,
            "market_after": market_after,
            "already_present": already_present,
            "added": added,
            "skipped": skipped,
        }
    )


def command_market_create(args: argparse.Namespace) -> None:
    context = resolve_context(args)
    require_scopes(context, "write_markets")
    country_codes = normalize_country_codes(args.country_code)
    mutation = """
    mutation MarketCreate($input: MarketCreateInput!) {
      marketCreate(input: $input) {
        market {
          id
          name
          regions(first: 250) {
            nodes {
              __typename
              name
              ... on MarketRegionCountry {
                id
                code
              }
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
    input_payload: dict[str, object] = {
        "name": args.name,
        "conditions": {
            "regionsCondition": {
                "regions": [{"countryCode": code} for code in country_codes],
            }
        },
        "status": args.status,
    }
    if args.handle:
        input_payload["handle"] = args.handle
    variables = {"input": input_payload}
    preview = {
        "name": args.name,
        "handle": args.handle,
        "status": args.status,
        "country_codes": country_codes,
    }
    if getattr(args, "dry_run", False):
        output(
            {
                "ok": True,
                "mode": args.command,
                "dry_run": True,
                "shop": context["store_domain"],
                "api_version": context["api_version"],
                "preview": preview,
                "query": mutation,
                "variables": variables,
            }
        )
        return
    data = graph_ql(context, mutation, variables)
    payload = data.get("marketCreate") or {}
    output(
        {
            "ok": True,
            "mode": args.command,
            "shop": context["store_domain"],
            "api_version": context["api_version"],
            "preview": preview,
            "market": serialize_market_record(payload.get("market") or {}),
            "user_errors": payload.get("userErrors") or [],
        }
    )


def command_graphql_query(args: argparse.Namespace) -> None:
    query = read_query_argument(args)
    variables = parse_json_value(args.variables_json, dict, "variables_json") or {}
    graphql_operation(args, query, variables)


def command_graphql_mutation(args: argparse.Namespace) -> None:
    query = read_query_argument(args)
    variables = parse_json_value(args.variables_json, dict, "variables_json") or {}
    graphql_operation(args, query, variables)


def add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")


def add_write_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser(description="Shopify Admin operations helper for OpenClaw")
    add_context_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    context = subparsers.add_parser("context", help="Show resolved Shopify context without exposing secrets")
    context.set_defaults(func=command_context)

    ping = subparsers.add_parser("ping", help="Check Shopify Admin connectivity")
    ping.set_defaults(func=command_ping)

    shop_info = subparsers.add_parser("shop-info", help="Read the main shop profile")
    shop_info.set_defaults(func=command_shop_info)

    orders_list = subparsers.add_parser("orders-list", help="List recent orders")
    orders_list.add_argument("--query")
    orders_list.add_argument("--first", type=int, default=20)
    orders_list.set_defaults(func=command_orders_list)

    order_get = subparsers.add_parser("order-get", help="Read one order by id or name")
    order_get.add_argument("--order-id")
    order_get.add_argument("--order-name")
    order_get.set_defaults(func=command_order_get)

    order_update = subparsers.add_parser("order-update", help="Update note, email, tags, or shipping address on an order")
    order_update.add_argument("--order-id")
    order_update.add_argument("--order-name")
    order_update.add_argument("--note")
    order_update.add_argument("--email")
    order_update.add_argument("--tags", action="append")
    order_update.add_argument("--shipping-address-json")
    add_write_flag(order_update)
    order_update.set_defaults(func=command_order_update)

    order_mark_paid = subparsers.add_parser("order-mark-paid", help="Mark an order as paid")
    order_mark_paid.add_argument("--order-id")
    order_mark_paid.add_argument("--order-name")
    add_write_flag(order_mark_paid)
    order_mark_paid.set_defaults(func=command_order_mark_paid)

    fulfillment_orders = subparsers.add_parser("fulfillment-orders-for-order", help="List fulfillment orders for a Shopify order")
    fulfillment_orders.add_argument("--order-id")
    fulfillment_orders.add_argument("--order-name")
    fulfillment_orders.set_defaults(func=command_fulfillment_orders_for_order)

    fulfillment_create = subparsers.add_parser("fulfillment-create", help="Create a fulfillment from a FulfillmentInput payload")
    fulfillment_create.add_argument("--input-json", required=True)
    fulfillment_create.add_argument("--message")
    add_write_flag(fulfillment_create)
    fulfillment_create.set_defaults(func=command_fulfillment_create)

    products_search = subparsers.add_parser("products-search", help="Search products")
    products_search.add_argument("--query")
    products_search.add_argument("--first", type=int, default=20)
    products_search.add_argument("--limit", dest="first", type=int)
    products_search.set_defaults(func=command_products_search)

    products_by_sku = subparsers.add_parser("products-by-sku", help="Read product variants by SKU")
    products_by_sku.add_argument("--sku", action="append", required=True)
    products_by_sku.add_argument("--first", type=int, default=20)
    products_by_sku.add_argument("--limit", dest="first", type=int)
    products_by_sku.set_defaults(func=command_products_by_sku)

    product_get = subparsers.add_parser("product-get", help="Read one product by id, handle, or SKU")
    product_get.add_argument("--product-id")
    product_get.add_argument("--handle")
    product_get.add_argument("--sku")
    product_get.set_defaults(func=command_product_get)

    product_update = subparsers.add_parser("product-update", help="Update a product with simple fields or raw ProductUpdateInput")
    product_update.add_argument("--product-id")
    product_update.add_argument("--handle")
    product_update.add_argument("--input-json")
    product_update.add_argument("--title")
    product_update.add_argument("--status")
    product_update.add_argument("--vendor")
    product_update.add_argument("--product-type")
    product_update.add_argument("--tags", action="append")
    add_write_flag(product_update)
    product_update.set_defaults(func=command_product_update)

    delivery_profiles_list = subparsers.add_parser("delivery-profiles-list", help="List Shopify delivery profiles")
    delivery_profiles_list.add_argument("--first", type=int, default=50)
    delivery_profiles_list.set_defaults(func=command_delivery_profiles_list)

    variants_shippable_list = subparsers.add_parser("variants-shippable-list", help="List shippable variants and optionally compare their delivery profile")
    variants_shippable_list.add_argument("--first", type=int, default=100)
    variants_shippable_list.add_argument("--limit", type=int)
    variants_shippable_list.add_argument("--query")
    variants_shippable_list.add_argument("--profile-id")
    variants_shippable_list.add_argument("--profile-name")
    variants_shippable_list.add_argument("--only-matching", action="store_true")
    variants_shippable_list.add_argument("--only-mismatched", action="store_true")
    variants_shippable_list.set_defaults(func=command_variants_shippable_list)

    delivery_profile_assign_variants = subparsers.add_parser("delivery-profile-assign-variants", help="Associate shippable variants to a delivery profile in batches")
    delivery_profile_assign_variants.add_argument("--profile-id")
    delivery_profile_assign_variants.add_argument("--profile-name")
    delivery_profile_assign_variants.add_argument("--variant-id", action="append")
    delivery_profile_assign_variants.add_argument("--query")
    delivery_profile_assign_variants.add_argument("--first", type=int, default=100)
    delivery_profile_assign_variants.add_argument("--limit", type=int)
    delivery_profile_assign_variants.add_argument("--batch-size", type=int, default=200)
    add_write_flag(delivery_profile_assign_variants)
    delivery_profile_assign_variants.set_defaults(func=command_delivery_profile_assign_variants)

    inventory_by_sku = subparsers.add_parser("inventory-by-sku", help="Read inventory by SKU")
    inventory_by_sku.add_argument("--sku", action="append", required=True)
    inventory_by_sku.add_argument("--first", type=int, default=20)
    inventory_by_sku.add_argument("--limit", dest="first", type=int)
    inventory_by_sku.set_defaults(func=command_inventory_by_sku)

    inventory_adjust = subparsers.add_parser("inventory-adjust", help="Adjust inventory from a raw InventoryAdjustQuantitiesInput payload")
    inventory_adjust.add_argument("--input-json", required=True)
    add_write_flag(inventory_adjust)
    inventory_adjust.set_defaults(func=command_inventory_adjust)

    variant_logistics_get = subparsers.add_parser("variant-logistics-get", help="Read weight and Openclaw logistics metafields for a variant")
    variant_logistics_get.add_argument("--variant-id")
    variant_logistics_get.add_argument("--sku")
    variant_logistics_get.add_argument("--handle")
    variant_logistics_get.set_defaults(func=command_variant_logistics_get)

    variant_logistics_set = subparsers.add_parser("variant-logistics-set", help="Update variant shipping weight and Openclaw logistics metafields")
    variant_logistics_set.add_argument("--variant-id")
    variant_logistics_set.add_argument("--sku")
    variant_logistics_set.add_argument("--handle")
    variant_logistics_set.add_argument("--weight-kg", type=float)
    variant_logistics_set.add_argument("--net-weight-kg", type=float)
    variant_logistics_set.add_argument("--length-cm", type=float)
    variant_logistics_set.add_argument("--width-cm", type=float)
    variant_logistics_set.add_argument("--height-cm", type=float)
    variant_logistics_set.add_argument("--packaging-type")
    add_write_flag(variant_logistics_set)
    variant_logistics_set.set_defaults(func=command_variant_logistics_set)

    customer_get = subparsers.add_parser("customer-get", help="Read a customer by id/email or search customers")
    customer_get.add_argument("--customer-id")
    customer_get.add_argument("--email")
    customer_get.add_argument("--query")
    customer_get.add_argument("--first", type=int, default=20)
    customer_get.add_argument("--limit", dest="first", type=int)
    customer_get.set_defaults(func=command_customer_get)

    customer_update = subparsers.add_parser("customer-update", help="Update a customer from a raw CustomerInput payload")
    customer_update.add_argument("--input-json", required=True)
    add_write_flag(customer_update)
    customer_update.set_defaults(func=command_customer_update)

    markets_list = subparsers.add_parser("markets-list", help="List Shopify markets with assigned country codes")
    markets_list.add_argument("--first", type=int, default=20)
    add_write_flag(markets_list)
    markets_list.set_defaults(func=command_markets_list)

    market_get = subparsers.add_parser("market-get", help="Read one Shopify market by id or exact name")
    market_get.add_argument("--market-id")
    market_get.add_argument("--market-name")
    market_get.set_defaults(func=command_market_get)

    market_create = subparsers.add_parser("market-create", help="Create a Shopify market with assigned country codes")
    market_create.add_argument("--name", required=True)
    market_create.add_argument("--handle")
    market_create.add_argument("--status", choices=["ACTIVE", "DRAFT"], default="DRAFT")
    market_create.add_argument("--country-code", action="append", required=True)
    add_write_flag(market_create)
    market_create.set_defaults(func=command_market_create)

    market_countries_add = subparsers.add_parser("market-countries-add", help="Add country codes to an existing Shopify market")
    market_countries_add.add_argument("--market-id")
    market_countries_add.add_argument("--market-name")
    market_countries_add.add_argument("--country-code", action="append", required=True)
    add_write_flag(market_countries_add)
    market_countries_add.set_defaults(func=command_market_countries_update)

    market_countries_ensure = subparsers.add_parser("market-countries-ensure", help="Ensure country codes exist on a Shopify market and skip unsupported ones with a report")
    market_countries_ensure.add_argument("--market-id")
    market_countries_ensure.add_argument("--market-name")
    market_countries_ensure.add_argument("--country-code", action="append", required=True)
    add_write_flag(market_countries_ensure)
    market_countries_ensure.set_defaults(func=command_market_countries_ensure)

    market_countries_remove = subparsers.add_parser("market-countries-remove", help="Remove country codes from an existing Shopify market")
    market_countries_remove.add_argument("--market-id")
    market_countries_remove.add_argument("--market-name")
    market_countries_remove.add_argument("--country-code", action="append", required=True)
    add_write_flag(market_countries_remove)
    market_countries_remove.set_defaults(func=command_market_countries_update)

    graphql_query = subparsers.add_parser("graphql-query", help="Run a raw GraphQL query")
    graphql_query.add_argument("--query")
    graphql_query.add_argument("--query-file")
    graphql_query.add_argument("--variables-json")
    graphql_query.set_defaults(func=command_graphql_query)

    graphql_mutation = subparsers.add_parser("graphql-mutation", help="Run a raw GraphQL mutation")
    graphql_mutation.add_argument("--query")
    graphql_mutation.add_argument("--query-file")
    graphql_mutation.add_argument("--variables-json")
    add_write_flag(graphql_mutation)
    graphql_mutation.set_defaults(func=command_graphql_mutation)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
