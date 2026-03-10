---
name: openclaw-shopify-fulfillment
description: Implement and maintain the Openclaw to Shopify fulfillment mirror integration. Use when Codex needs to connect Openclaw to Shopify, synchroniser des commandes pretes a expedier, add or update a Shopify webhook endpoint, verify Shopify HMAC signatures, parse orders or fulfillments payloads, fetch missing shipping data from the Shopify Admin API with read-only scopes, or map Shopify shipping data into Openclaw logistics records.
---

# Openclaw Shopify Fulfillment

## Overview

Implement the mirror strategy where Shopify stays the source of truth and Openclaw consumes ready-to-process logistics data.
Keep the integration read-only on the Shopify side, acknowledge webhooks quickly, normalize payloads into an Openclaw envelope, and enrich missing data through the Admin API only when required.

## Core Workflow

1. Confirm runtime prerequisites.
- Read `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_WEBHOOK_SECRET`, `SHOPIFY_API_VERSION`, and the Openclaw storage or queue settings from the target environment.
- Refuse to hardcode tokens or secrets.
- Keep Shopify access read-only unless the project explicitly expands scopes.

2. Receive the webhook.
- Expose a `POST` endpoint only.
- Capture the raw request body before JSON parsing. Shopify HMAC verification must use the raw bytes.
- Read at minimum `X-Shopify-Topic`, `X-Shopify-Hmac-Sha256`, `X-Shopify-Shop-Domain`, `X-Shopify-Webhook-Id`, and `X-Shopify-Event-Id`.

3. Verify authenticity first.
- Validate `X-Shopify-Hmac-Sha256` with HMAC-SHA256 over the raw body and the shared secret.
- Treat header names as case-insensitive.
- Reject invalid signatures before JSON parsing or persistence.

4. Normalize the payload.
- Use `orders/paid` as the early order signal.
- Use `fulfillments/create` as the later shipment signal with tracking data.
- Map the webhook into the Openclaw envelope described in [references/integration-contract.md](references/integration-contract.md).
- Keep printable label URLs optional because the official standard webhook payloads expose tracking data more reliably than label documents.

5. Enrich only when necessary.
- Prefer the GraphQL Admin API for new work.
- Keep REST only as a fallback when the host project already depends on legacy REST endpoints.
- Use read-only enrichment when shipping address fields, tracking fields, or other logistics data are missing from the webhook.
- Read [references/shopify-api-notes.md](references/shopify-api-notes.md) before choosing webhook topics or fallback queries.

6. Persist and expose in Openclaw.
- Store the raw webhook metadata separately from the normalized operational record.
- Deduplicate event processing with `X-Shopify-Event-Id`.
- Upsert the operator-facing record with a stable key built from shop domain, order id, and fulfillment id when present.
- Show `print_label_url` only when a real URL exists. Otherwise store tracking URLs and mark the label as pending.

7. Acknowledge quickly and process asynchronously.
- Return a 2xx response after the webhook is durably queued or stored.
- Keep enrichment calls, PDF lookups, and heavy database fan-out out of the request path.
- Assume duplicate delivery and out-of-order delivery are possible.

## Delivery Rules

- Prefer pure helpers for HMAC verification and payload normalization so they can be unit-tested outside the web framework.
- Keep Openclaw as a consumer of Shopify events. Do not mutate orders, fulfillments, products, or themes unless the project requirements and scopes change.
- Treat `orders/paid` as "payment succeeded", not as a universal guarantee that the order is ready to ship. If the project later needs the true Shopify fulfillment-order lifecycle, plan a scope review first.
- Log topic, shop domain, webhook id, event id, order id, fulfillment id, and normalization outcome for every delivery.
- Preserve the raw payload for audit and replay when a normalization bug is fixed.

## Resources

- Use [scripts/verify_shopify_hmac.py](scripts/verify_shopify_hmac.py) to compute or verify Shopify webhook signatures from a raw payload file.
- Use [scripts/normalize_shopify_webhook.py](scripts/normalize_shopify_webhook.py) to transform webhook payload fixtures into the Openclaw mirror envelope.
- Read [references/integration-contract.md](references/integration-contract.md) for the normalized schema, idempotency keys, and storage rules.
- Read [references/shopify-api-notes.md](references/shopify-api-notes.md) for the current Shopify webhook and Admin API guidance verified on 2026-03-10.
- Use the JSON fixtures in `assets/` to smoke-test normalization changes before patching application code.

## Example Requests

- "Add a secure Shopify webhook endpoint to Openclaw and reject invalid HMAC signatures."
- "Map `orders/paid` into our logistics queue without touching Shopify data."
- "Parse `fulfillments/create` and surface carrier plus tracking URLs in Openclaw."
- "Use the Shopify Admin API in read-only mode to fill missing shipping data after webhook intake."
