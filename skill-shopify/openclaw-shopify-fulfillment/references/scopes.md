# Shopify Scopes and Approval Matrix

Use this file to map an Openclaw task to the minimum Shopify access required.

## Current Principle

Start with the minimum viable bundle for the requested capability.
If the task requires analytics, protected customer data, or theme file mutation, explicitly note the extra approval risk before implementation.

## Capability Bundles

### Fulfillment Mirror

- `read_orders`
- `read_fulfillments`

Optional expansion:

- `read_all_orders`
- `read_shipping`

### Commerce Operations

- `read_orders`
- `write_orders`
- `read_fulfillments`
- `write_fulfillments`
- `read_inventory`
- `write_inventory`
- `read_locations`
- `write_locations`
- `read_returns`
- `write_returns`
- `read_shipping`
- `write_shipping`

### Catalog Operations

- `read_products`
- `write_products`
- `read_inventory`
- `write_inventory`
- `read_files`
- `write_files`
- `read_metaobjects`
- `write_metaobjects`
- `read_metaobject_definitions`
- `write_metaobject_definitions`

### SEO and Content

- `read_content`
- `write_content`
- `read_online_store_navigation`
- `write_online_store_navigation`
- `read_metaobjects`
- `write_metaobjects`
- `read_metaobject_definitions`
- `write_metaobject_definitions`
- `read_files`
- `write_files`
- `read_translations`
- `write_translations`
- `read_locales`
- `write_locales`

### Marketing and Analytics

- `read_reports`
- `read_marketing_events`
- `write_marketing_events`
- `read_discounts`
- `write_discounts`
- `read_price_rules`
- `write_price_rules`
- `write_pixels`
- `read_customer_events`

Use this bundle before claiming:

- conversion rate
- traffic breakdown
- funnel metrics
- campaign attribution

### Design and Storefront

- `read_themes`
- `write_themes`
- `read_files`
- `write_files`
- `read_content`
- `write_content`
- `read_checkout_branding_settings`
- `write_checkout_branding_settings`

Direct theme file writes can require more than the raw scope. Verify the current Shopify requirement before implementation.

### Customers and CRM

- `read_customers`
- `write_customers`

Potential protected customer data scopes:

- `read_customer_name`
- `read_customer_email`
- `read_customer_phone`
- `read_customer_address`

Do not assume these are available or approved on a production store.

### Markets and International

- `read_markets`
- `write_markets`
- `read_translations`
- `write_translations`
- `read_locales`
- `write_locales`

## High-Risk Scopes

Escalate carefully when asking for:

- `read_reports`
- `read_all_orders`
- `write_themes`
- protected customer data scopes

These can affect app review, privacy posture, or the ability to install the app widely.

## Task-to-Scope Checklist

Before implementing any new Openclaw action, write down:

1. What resource is being read or mutated.
2. Whether the task requires analytics or customer PII.
3. Whether the task changes production storefront behavior.
4. The minimum scope bundle needed.
5. Whether a preview or approval is mandatory before write execution.
