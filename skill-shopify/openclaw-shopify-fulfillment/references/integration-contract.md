# Openclaw Shopify Integration Contract

## Goal

Mirror Shopify order and fulfillment data into Openclaw without writing back to Shopify.
Create one durable intake path for webhook deliveries and one normalized record shape for Openclaw operators.

## Runtime Inputs

- `SHOPIFY_STORE_DOMAIN`: Store domain such as `example.myshopify.com`
- `SHOPIFY_CLIENT_ID`: App client id used to mint a short-lived Admin API token
- `SHOPIFY_CLIENT_SECRET`: App client secret used to mint a short-lived Admin API token
- `SHOPIFY_WEBHOOK_SECRET`: Shared secret used for HMAC verification
- `SHOPIFY_API_VERSION`: Versioned Admin API path segment such as `2026-01`
- `OPENCLAW_DB_DSN` or repository configuration: Destination for normalized records
- `OPENCLAW_QUEUE_NAME` or equivalent async transport: Durable background processing
- `OPENCLAW_LABEL_URL_FIELD` optional: Project-specific field used if label documents are stored outside standard Shopify payloads

## Headers To Persist

- `X-Shopify-Topic`
- `X-Shopify-Shop-Domain`
- `X-Shopify-Hmac-Sha256`
- `X-Shopify-Webhook-Id`
- `X-Shopify-Event-Id`
- `X-Shopify-API-Version`
- `X-Shopify-Triggered-At`

Persist the raw header values with the raw payload so failed deliveries can be replayed after parser fixes.

## Supported Topics

### `orders/paid`

- Use as the first signal that a paid order exists.
- Expect order identity, customer email, shipping address, and line items.
- Do not assume tracking data or printable labels exist yet.

### `fulfillments/create`

- Use as the shipment signal after Shopify creates a fulfillment.
- Expect fulfillment id, order id, destination address, carrier, and tracking fields.
- Treat printable label URLs as optional enrichment data, not a guaranteed standard field.

## Normalized Envelope

```json
{
  "source": "shopify",
  "strategy": "mirror",
  "topic": "fulfillments/create",
  "shop_domain": "example.myshopify.com",
  "event_key": "example.myshopify.com:98880550-7158-44d4-b7cd-2c97c8a091b5",
  "delivery_key": "example.myshopify.com:b54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
  "triggered_at": "2026-03-10T12:00:00Z",
  "order": {
    "id": "820982911946154508",
    "gid": "gid://shopify/Order/820982911946154508",
    "reference": "#1001"
  },
  "fulfillment": {
    "id": "123456",
    "tracking_urls": [
      "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    ]
  },
  "openclaw": {
    "record_key": "example.myshopify.com:820982911946154508:123456",
    "operator_action": "track_shipment",
    "needs_api_enrichment": false
  }
}
```

## Field Mapping

| Shopify source | Openclaw field | Notes |
| --- | --- | --- |
| `X-Shopify-Event-Id` | `event_key` | Primary dedupe key |
| `X-Shopify-Webhook-Id` | `delivery_key` | Delivery log key |
| `payload.id` on `orders/paid` | `order.id` | Convert to string for storage consistency |
| `payload.order_id` on `fulfillments/create` | `order.id` | Same order key across topics |
| `payload.admin_graphql_api_id` | `order.gid` or `fulfillment.gid` | Preserve GraphQL ids when present |
| `payload.name` | `order.reference` | Human-visible order number |
| `payload.shipping_address` or `payload.destination` | `order.shipping_address` | Normalize into one address shape |
| `payload.line_items[*]` | `order.items[*]` | Preserve sku, title, quantity, variant id |
| `payload.tracking_number` and `payload.tracking_numbers` | `fulfillment.tracking_numbers` | Keep as an array |
| `payload.tracking_url` and `payload.tracking_urls` | `fulfillment.tracking_urls` | Keep as an array |
| `payload.tracking_company` | `fulfillment.carrier` | Surface to operators |
| Project-specific label document field | `fulfillment.label_url` | Optional |

## Persistence Rules

- Store the raw payload plus headers in an append-only intake table or blob store.
- Deduplicate processing by `event_key`.
- Upsert the operator-facing record by `record_key = {shop_domain}:{order_id}:{fulfillment_id or order}`.
- Keep `label_url` nullable. Do not invent placeholder URLs.
- Preserve `needs_api_enrichment` and `missing_fields` so a background job can recover incomplete records.

## Failure Handling

- Reject invalid HMAC signatures before parsing or persistence.
- Reject malformed JSON only after storing minimal delivery metadata when possible.
- Retry transient Openclaw or Shopify API failures internally after durable intake.
- Keep records readable in Openclaw even when enrichment fails. Expose the missing fields instead of dropping the order.
