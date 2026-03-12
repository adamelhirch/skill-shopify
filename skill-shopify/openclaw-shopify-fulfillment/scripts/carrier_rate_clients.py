import base64
import json
import os
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


UPS_SERVICE_CODE_MAP = {
    "UPS_STANDARD": "11",
    "UPS_EXPEDITED": "08",
    "UPS_SAVER": "65",
    "UPS_EXPRESS": "07",
    "UPS_EXPRESS_PLUS": "54",
}

CHRONOPOST_SERVICE_CODE_MAP = {
    "CHRONO13": "13",
    "CHRONO18": "18",
    "CHRONO10": "10",
    "CHRONO_RELAIS": "86",
}

COLISSIMO_PRODUCT_CODE_MAP = {
    "COLISSIMO_DOMICILE_ACCESS": "DOM",
}

COUNTRY_NAME_TO_CODE = {
    "france": "FR",
    "belgique": "BE",
    "belgium": "BE",
    "espagne": "ES",
    "spain": "ES",
    "italie": "IT",
    "italy": "IT",
    "allemagne": "DE",
    "germany": "DE",
    "portugal": "PT",
    "pays-bas": "NL",
    "netherlands": "NL",
    "royaume-uni": "GB",
    "united kingdom": "GB",
    "suisse": "CH",
    "switzerland": "CH",
    "etats-unis": "US",
    "united states": "US",
}


class CarrierRateError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_provider(carrier_name: str | None) -> str:
    return str(carrier_name or "").strip().lower()


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_first_text(root: ET.Element, local_name: str) -> str | None:
    for node in root.iter():
        if _local_name(node.tag) == local_name:
            text = (node.text or "").strip()
            if text:
                return text
    return None


def _find_all_text(root: ET.Element, local_name: str) -> list[str]:
    values: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) != local_name:
            continue
        text = (node.text or "").strip()
        if text:
            values.append(text)
    return values


def _recipient_country_code(recipient: dict | None) -> str:
    if not recipient:
        return "FR"
    country_code = str(recipient.get("countryCodeV2") or "").strip().upper()
    if country_code:
        return country_code
    country_name = str(recipient.get("country") or "").strip().lower()
    if country_name in COUNTRY_NAME_TO_CODE:
        return COUNTRY_NAME_TO_CODE[country_name]
    return "FR"


def _recipient_zip(recipient: dict | None) -> str:
    if not recipient:
        return ""
    return str(recipient.get("zip") or "").strip()


def _http_post_json(url: str, payload: dict, headers: dict[str, str], timeout_sec: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CarrierRateError(f"HTTP {exc.code} on {url}: {body}") from exc
    except OSError as exc:
        raise CarrierRateError(f"Connection error on {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CarrierRateError(f"Invalid JSON response from {url}: {exc}") from exc


def _http_post_form(url: str, payload: dict[str, str], headers: dict[str, str], timeout_sec: int) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CarrierRateError(f"HTTP {exc.code} on {url}: {body}") from exc
    except OSError as exc:
        raise CarrierRateError(f"Connection error on {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CarrierRateError(f"Invalid JSON response from {url}: {exc}") from exc


def _http_post_xml(url: str, xml_body: str, headers: dict[str, str], timeout_sec: int) -> ET.Element:
    request = urllib.request.Request(
        url,
        data=xml_body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CarrierRateError(f"HTTP {exc.code} on {url}: {body}") from exc
    except OSError as exc:
        raise CarrierRateError(f"Connection error on {url}: {exc}") from exc
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CarrierRateError(f"Invalid XML response from {url}: {exc}") from exc


def _resolve_service_code(
    service: dict,
    override_field: str,
    map_table: dict[str, str],
) -> str | None:
    explicit = str(service.get(override_field) or "").strip()
    if explicit:
        return explicit
    raw = str(service.get("carrier_service_code") or "").strip()
    if not raw:
        return None
    if raw in map_table:
        return map_table[raw]
    return raw


def _quote_ups(service: dict, parcel: dict, recipient: dict | None, timeout_sec: int) -> dict:
    client_id = _env("UPS_CLIENT_ID")
    client_secret = _env("UPS_CLIENT_SECRET")
    account_number = _env("UPS_ACCOUNT_NUMBER") or _env("UPS_SHIPPER_NUMBER")
    if not client_id or not client_secret or not account_number:
        raise CarrierRateError("Missing UPS credentials (UPS_CLIENT_ID, UPS_CLIENT_SECRET, UPS_ACCOUNT_NUMBER)")

    base_url = _env("UPS_BASE_URL", "https://onlinetools.ups.com")
    token_path = _env("UPS_TOKEN_PATH", "/security/v1/oauth/token")
    rate_path = _env("UPS_RATE_PATH", "/api/rating/v2403/Rate")
    token_url = f"{base_url.rstrip('/')}{token_path}"
    rate_url = f"{base_url.rstrip('/')}{rate_path}"

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    token_data = _http_post_form(
        token_url,
        {"grant_type": "client_credentials"},
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
        },
        timeout_sec,
    )
    access_token = token_data.get("access_token")
    if not access_token:
        raise CarrierRateError("UPS token response missing access_token")

    service_code = _resolve_service_code(service, "carrier_service_api_code", UPS_SERVICE_CODE_MAP)
    if not service_code:
        raise CarrierRateError("Missing UPS service code (carrier_service_code or carrier_service_api_code)")

    shipper_country = _env("UPS_SHIPPER_COUNTRY_CODE", "FR")
    shipper_postal = _env("UPS_SHIPPER_POSTAL_CODE")
    shipper_city = _env("UPS_SHIPPER_CITY", "Bessan")
    shipper_address1 = _env("UPS_SHIPPER_ADDRESS1", "Bessan")
    if not shipper_postal:
        raise CarrierRateError("Missing UPS_SHIPPER_POSTAL_CODE")

    recipient_country = _recipient_country_code(recipient)
    recipient_postal = _recipient_zip(recipient)
    recipient_city = str((recipient or {}).get("city") or "").strip() or "Unknown"
    recipient_address1 = str((recipient or {}).get("address1") or "").strip() or "Unknown"

    weight_kg = _float_or_none(parcel.get("weight_kg"))
    length_cm = _float_or_none(parcel.get("length_cm"))
    width_cm = _float_or_none(parcel.get("width_cm"))
    height_cm = _float_or_none(parcel.get("height_cm"))
    if not weight_kg or not length_cm or not width_cm or not height_cm:
        raise CarrierRateError("Missing parcel dimensions/weight for UPS quote")

    payload = {
        "RateRequest": {
            "Request": {
                "RequestOption": "Rate",
                "TransactionReference": {
                    "CustomerContext": f"openclaw-{uuid.uuid4()}",
                },
            },
            "Shipment": {
                "Shipper": {
                    "Name": _env("UPS_SHIPPER_NAME", "Openclaw"),
                    "ShipperNumber": account_number,
                    "Address": {
                        "AddressLine": [shipper_address1],
                        "City": shipper_city,
                        "PostalCode": shipper_postal,
                        "CountryCode": shipper_country,
                    },
                },
                "ShipFrom": {
                    "Name": _env("UPS_SHIPFROM_NAME", _env("UPS_SHIPPER_NAME", "Openclaw")),
                    "Address": {
                        "AddressLine": [shipper_address1],
                        "City": shipper_city,
                        "PostalCode": shipper_postal,
                        "CountryCode": shipper_country,
                    },
                },
                "ShipTo": {
                    "Name": f"{str((recipient or {}).get('firstName') or '').strip()} {str((recipient or {}).get('lastName') or '').strip()}".strip()
                    or "Client",
                    "Address": {
                        "AddressLine": [recipient_address1],
                        "City": recipient_city,
                        "PostalCode": recipient_postal,
                        "CountryCode": recipient_country,
                    },
                },
                "Service": {"Code": service_code},
                "Package": [
                    {
                        "PackagingType": {"Code": _env("UPS_PACKAGING_CODE", "02")},
                        "Dimensions": {
                            "UnitOfMeasurement": {"Code": "CM"},
                            "Length": f"{length_cm:.2f}",
                            "Width": f"{width_cm:.2f}",
                            "Height": f"{height_cm:.2f}",
                        },
                        "PackageWeight": {
                            "UnitOfMeasurement": {"Code": "KGS"},
                            "Weight": f"{weight_kg:.3f}",
                        },
                    }
                ],
            },
        }
    }

    rate_data = _http_post_json(
        rate_url,
        payload,
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "transId": str(uuid.uuid4()),
            "transactionSrc": "openclaw",
        },
        timeout_sec,
    )

    rate_response = rate_data.get("RateResponse") or rate_data.get("rateResponse") or {}
    rated = rate_response.get("RatedShipment") or rate_response.get("ratedShipment") or []
    if isinstance(rated, dict):
        rated_shipments = [rated]
    else:
        rated_shipments = rated if isinstance(rated, list) else []
    if not rated_shipments:
        raise CarrierRateError("UPS response did not include RatedShipment")
    first = rated_shipments[0]
    charges = (
        ((first.get("NegotiatedRateCharges") or {}).get("TotalCharge"))
        or first.get("TotalCharges")
        or {}
    )
    amount = _float_or_none(charges.get("MonetaryValue"))
    currency_code = str(charges.get("CurrencyCode") or "EUR")
    if amount is None:
        raise CarrierRateError("UPS response missing charge amount")

    return {
        "status": "ok",
        "provider": "ups",
        "service_code": service_code,
        "amount": amount,
        "currency_code": currency_code,
        "source": "ups-rating-api",
    }


def _quote_chronopost(service: dict, parcel: dict, recipient: dict | None, timeout_sec: int) -> dict:
    account_number = _env("CHRONOPOST_ACCOUNT_NUMBER")
    password = _env("CHRONOPOST_PASSWORD")
    if not account_number or not password:
        raise CarrierRateError("Missing Chronopost credentials (CHRONOPOST_ACCOUNT_NUMBER, CHRONOPOST_PASSWORD)")

    endpoint = _env("CHRONOPOST_QUICKCOST_URL", "https://ws.chronopost.fr/quickcost-cxf/QuickcostServiceWS")
    operation = _env("CHRONOPOST_QUICKCOST_OPERATION", "quickCostV3")
    type_code = _env("CHRONOPOST_QUICKCOST_TYPE", "M")
    dep_code = _env("CHRONOPOST_SHIPPER_POSTAL_CODE")
    if not dep_code:
        raise CarrierRateError("Missing CHRONOPOST_SHIPPER_POSTAL_CODE")

    arr_code = _recipient_zip(recipient)
    if not arr_code:
        raise CarrierRateError("Recipient postal code is required for Chronopost quote")

    weight_kg = _float_or_none(parcel.get("weight_kg"))
    if not weight_kg:
        raise CarrierRateError("Missing parcel weight for Chronopost quote")

    product_code = _resolve_service_code(service, "carrier_service_api_code", CHRONOPOST_SERVICE_CODE_MAP)
    if not product_code:
        raise CarrierRateError("Missing Chronopost product code (carrier_service_code or carrier_service_api_code)")

    request_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cxf="http://cxf.quickcost.soap.chronopost.fr/">
  <soapenv:Header/>
  <soapenv:Body>
    <cxf:{operation}>
      <accountNumber>{account_number}</accountNumber>
      <password>{password}</password>
      <depCode>{dep_code}</depCode>
      <arrCode>{arr_code}</arrCode>
      <weight>{weight_kg:.3f}</weight>
      <productCode>{product_code}</productCode>
      <type>{type_code}</type>
    </cxf:{operation}>
  </soapenv:Body>
</soapenv:Envelope>
"""
    root = _http_post_xml(
        endpoint,
        request_body,
        {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "",
        },
        timeout_sec,
    )

    error_code = _find_first_text(root, "errorCode")
    if error_code and error_code not in {"0", "0.0"}:
        error_message = _find_first_text(root, "errorMessage") or "Unknown Chronopost error"
        raise CarrierRateError(f"Chronopost quickCost error {error_code}: {error_message}")

    amount_ttc = _float_or_none(_find_first_text(root, "amountTTC"))
    amount = _float_or_none(_find_first_text(root, "amount"))
    resolved_amount = amount_ttc if amount_ttc is not None else amount
    if resolved_amount is None:
        raise CarrierRateError("Chronopost response missing amount/amountTTC")

    return {
        "status": "ok",
        "provider": "chronopost",
        "service_code": product_code,
        "amount": resolved_amount,
        "currency_code": _env("CHRONOPOST_CURRENCY_CODE", "EUR"),
        "source": f"chronopost-{operation}",
    }


def _quote_colissimo(service: dict, recipient: dict | None, timeout_sec: int) -> dict:
    contract_number = _env("COLISSIMO_CONTRACT_NUMBER")
    password = _env("COLISSIMO_PASSWORD")
    if not contract_number or not password:
        raise CarrierRateError("Missing Colissimo credentials (COLISSIMO_CONTRACT_NUMBER, COLISSIMO_PASSWORD)")

    endpoint = _env("COLISSIMO_SLS_URL", "https://ws.colissimo.fr/sls-ws/SlsServiceWS/2.0")
    product_code = _resolve_service_code(service, "carrier_service_api_code", COLISSIMO_PRODUCT_CODE_MAP)
    if not product_code:
        raise CarrierRateError("Missing Colissimo product code (carrier_service_code or carrier_service_api_code)")

    country_code = _recipient_country_code(recipient)
    zip_code = _recipient_zip(recipient)
    city = str((recipient or {}).get("city") or "").strip()

    request_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:sls="http://sls.ws.coliposte.fr">
  <soapenv:Header/>
  <soapenv:Body>
    <sls:getProductInter>
      <getProductInterRequest>
        <contractNumber>{contract_number}</contractNumber>
        <password>{password}</password>
        <productCode>{product_code}</productCode>
        <countryCode>{country_code}</countryCode>
        <zipCode>{zip_code}</zipCode>
        <city>{city}</city>
      </getProductInterRequest>
    </sls:getProductInter>
  </soapenv:Body>
</soapenv:Envelope>
"""
    root = _http_post_xml(
        endpoint,
        request_body,
        {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "",
        },
        timeout_sec,
    )

    messages = _find_all_text(root, "messageContent")
    has_errors = bool(messages)

    return {
        "status": "unpriced",
        "provider": "colissimo",
        "service_code": product_code,
        "amount": None,
        "currency_code": "EUR",
        "source": "colissimo-getProductInter",
        "message": (
            "Colissimo SLS endpoint is connected, but this operation does not return a quote amount. "
            "Use fallback policy cost or a dedicated tariff source."
        ),
        "api_messages": messages if has_errors else [],
    }


def quote_service_rate(service: dict, parcel: dict, recipient: dict | None, timeout_sec: int = 20) -> dict:
    provider = _normalize_provider(service.get("carrier_name"))
    if not provider:
        return {
            "status": "skipped",
            "provider": None,
            "service_code": None,
            "amount": None,
            "currency_code": None,
            "source": "no-carrier-name",
            "message": "Service has no carrier_name; live quote skipped.",
        }
    try:
        if provider == "ups":
            return _quote_ups(service, parcel, recipient, timeout_sec)
        if provider == "chronopost":
            return _quote_chronopost(service, parcel, recipient, timeout_sec)
        if provider == "colissimo":
            return _quote_colissimo(service, recipient, timeout_sec)
        return {
            "status": "unsupported_provider",
            "provider": provider,
            "service_code": str(service.get("carrier_service_code") or "").strip() or None,
            "amount": None,
            "currency_code": None,
            "source": "unsupported-provider",
            "message": f"Provider '{provider}' is not implemented for live quote.",
        }
    except CarrierRateError as exc:
        return {
            "status": "error",
            "provider": provider,
            "service_code": str(service.get("carrier_service_code") or "").strip() or None,
            "amount": None,
            "currency_code": None,
            "source": "carrier-api-error",
            "message": str(exc),
        }
