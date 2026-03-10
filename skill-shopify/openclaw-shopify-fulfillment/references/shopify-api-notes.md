# Shopify API Notes

Verified against official Shopify documentation on 2026-03-10.

## Current Official Constraints

- Shopify webhook headers are case-insensitive.
- HTTPS webhook verification uses the raw request body and `X-Shopify-Hmac-Sha256`.
- Shopify expects a 2xx response in under five seconds for HTTPS webhooks.
- Shopify retries failed webhook deliveries up to 8 times over the next 4 hours.
- Shopify recommends using `X-Shopify-Event-Id` to detect duplicate webhook events.
- The REST Admin API has been legacy since 2024-10-01. New work should default to the GraphQL Admin API.
- ShopifyQL and analytics reporting require `read_reports`.
- Protected customer data access depends on explicit customer data scopes and Shopify approval posture.
- Direct theme file mutation can require more than raw theme scopes depending on the API surface used.

## Topic Guidance For This Skill

- `orders/paid`: Use as the early order intake signal. The payload includes order-level data and shipping address information.
- `fulfillments/create`: Use as the fulfillment signal. The payload includes fulfillment-level status, destination, carrier, and tracking fields.
- `fulfillment_orders/ready_to_fulfill`: Treat as a future upgrade path only. It is a better ready-to-ship signal for some fulfillment workflows, but it requires fulfillment-order scopes that are not in the current approved scope set.

## Header Checklist

Always capture and log:

- `X-Shopify-Topic`
- `X-Shopify-Hmac-Sha256`
- `X-Shopify-Shop-Domain`
- `X-Shopify-Webhook-Id`
- `X-Shopify-Event-Id`
- `X-Shopify-API-Version`
- `X-Shopify-Triggered-At`

## Admin API Direction

- Prefer the GraphQL Admin API for new implementations.
- Keep REST read-only fallbacks only when the target codebase already depends on legacy REST endpoints.
- Use `X-Shopify-Access-Token` for authenticated Admin API requests.
- For Dev Dashboard apps, mint the Admin API token with the client credentials grant and refresh it before the 24-hour expiry window.
- Use the versioned endpoint shape `https://{shop_domain}/admin/api/{api_version}/graphql.json`.

## Analytics Direction

- Use ShopifyQL only when `read_reports` is present.
- If `read_reports` is missing, present an order-performance report instead of a true conversion report.
- Do not infer traffic or conversion from orders alone without clearly labeling the limitation.

## Design Direction

- Prefer low-risk storefront changes first: content updates, files, branding settings, app blocks, and app embeds.
- Treat direct theme file writes as a protected operation with preview and rollback requirements.
- Keep checkout branding separate from theme work because it uses its own permission surface.

## Example GraphQL Read Query

Use a read-only query like the following to enrich a webhook with missing shipping or tracking data:

```graphql
query OpenclawOrderSync($orderId: ID!) {
  order(id: $orderId) {
    id
    name
    createdAt
    displayFinancialStatus
    displayFulfillmentStatus
    email
    contactEmail
    shippingAddress {
      address1
      address2
      city
      province
      provinceCode
      country
      countryCodeV2
      zip
      phone
      firstName
      lastName
      company
      name
    }
    lineItems(first: 50) {
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
    fulfillments(first: 10) {
      id
      status
      trackingInfo(first: 10) {
        company
        number
        url
      }
      originAddress {
        address1
        address2
        city
        provinceCode
        countryCode
        zip
      }
    }
  }
}
```

Adjust the selected fields if the host project pins a different API version.

## Read-Only REST Fallback

- `GET /admin/api/{version}/orders/{order_id}.json`
- `GET /admin/api/{version}/orders/{order_id}/fulfillments.json`

Use REST only if the host project already relies on it or the team has not migrated yet.

## Official References

- About webhooks: https://shopify.dev/docs/apps/build/webhooks
- HTTPS webhook delivery: https://shopify.dev/docs/apps/build/webhooks/subscribe/https
- Webhook reference: https://shopify.dev/docs/api/admin-rest/latest/resources/webhook
- Order object: https://shopify.dev/docs/api/admin-graphql/latest/objects/Order
- Order query examples: https://shopify.dev/docs/api/admin-graphql/latest/queries/order
- REST Admin API reference: https://shopify.dev/docs/api/admin-rest
