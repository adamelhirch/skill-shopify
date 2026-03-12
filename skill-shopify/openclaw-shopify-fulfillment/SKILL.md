---
name: openclaw-shopify-fulfillment
description: Operate and evolve an Openclaw-managed Shopify store across fulfillment, catalog, SEO, content, navigation, reports, marketing, analytics, customer support, discounts, markets, files, theme work, and checkout branding. Use when Codex needs to read or update Shopify data, plan required access scopes, add webhooks, verify HMAC signatures, analyze store performance, generate SEO or marketing actions, modify content structures, manage products or inventory, or coordinate design and storefront changes from Openclaw.
---

# Openclaw Shopify Operator

## Overview

Use this skill when Openclaw is expected to operate Shopify as the store control plane, not just as a fulfillment mirror.
Treat Shopify as the execution surface and Openclaw as the planner, operator, auditor, and reporting layer across commerce, growth, content, design, and support.

## Absolute Coverage Rule

This skill is not limited to the small set of convenience commands already exposed by helper scripts.
If Shopify grants access to a resource or mutation family, agents should assume the skill may operate that surface through raw GraphQL Admin API calls even when no dedicated wrapper command exists yet.

Use this rule:

- If a deterministic helper already exists, use it first.
- If no helper exists but the granted scopes should allow the operation, fall back to `graphql-query` or `graphql-mutation`.
- If the operation is high-risk, generate a preview or dry-run payload before execution.
- If Shopify rejects the call, treat the rejection as the source of truth and report the exact missing permission or invalid field.

## Capability Routing

Route every request into one primary capability before touching the API.

### 1. Commerce Ops

Use for orders, fulfillments, returns, shipping, stock, locations, and back-office workflows.

- Read and update orders only when the granted scopes allow it.
- Keep webhook intake idempotent.
- Preserve the original fulfillment mirror flow described in [references/integration-contract.md](references/integration-contract.md).
- Use HMAC verification for inbound webhooks.

### 2. Catalog Ops

Use for products, variants, collections, inventory, files, and product publishing.

- Prefer GraphQL Admin API for product and inventory work.
- Use metafields and metaobjects when the host project needs structured catalog enrichment.
- Preview bulk product updates before writing.

### 3. SEO and Content

Use for pages, blog articles, redirects, navigation, metafields, metaobjects, locales, and translations.

- Treat SEO changes as controlled publishing work, not ad hoc edits.
- Generate titles, meta descriptions, structured content, redirects, and internal linking plans before mutating store content.
- Persist content strategy in Openclaw so changes can be replayed and audited.

### 4. Marketing and Analytics

Use for reports, ShopifyQL, traffic analysis, discounts, pixels, customer events, and campaign attribution.

- Do not claim a conversion rate without analytics scopes and source data.
- Use `read_reports` before attempting ShopifyQL or traffic-based reporting.
- Split reporting from campaign execution. Report first, mutate second.

### 5. Design and Storefront

Use for theme reads, theme updates, app embeds, checkout branding, media, and visual merchandising.

- Prefer safe storefront patterns first: app blocks, theme app extensions, content updates, file changes, and branding settings.
- Treat direct theme file writes as protected operations because Shopify can require extra approval for theme file APIs.
- Always create a diff or preview before publishing visual changes.

### 6. Customer and CRM

Use for customer records, segmentation, customer support views, and lifecycle actions.

- Respect protected customer data requirements.
- Do not query customer names, emails, phones, or addresses unless the scopes and compliance posture explicitly allow it.
- Minimize data access to the fields needed for the task.

## Execution Model

1. Identify the capability.
2. Verify required scopes with [scripts/check_granted_scopes.py](scripts/check_granted_scopes.py).
3. Prefer read-only discovery first.
4. Generate a plan or diff for any meaningful write.
5. Execute through GraphQL Admin API by default.
6. Log the action, inputs, outputs, and rollback path in Openclaw.

## OpenClaw Runtime

At minimum, read these from the runtime:

- `SHOPIFY_STORE_DOMAIN`
- `SHOPIFY_CLIENT_ID`
- `SHOPIFY_CLIENT_SECRET`
- `SHOPIFY_API_VERSION`

Use these when the capability needs them:

- `SHOPIFY_SCOPE`
- `SHOPIFY_WEBHOOK_SECRET`
- `OPENCLAW_DB_DSN`
- `OPENCLAW_QUEUE_NAME`
- `OPENCLAW_AUDIT_SINK`
- `OPENCLAW_MEDIA_BUCKET`

Never hardcode secrets in source files, prompts, or fixtures.
Use the Shopify client credentials grant to mint short-lived Admin API access tokens at runtime instead of storing a long-lived `SHOPIFY_ACCESS_TOKEN` in the environment.

## Scope Rules

- Refuse silent writes when scopes are missing or ambiguous.
- Refuse analytics claims when traffic scopes are missing.
- Refuse theme file mutation when the store lacks the needed permission or exemption path.
- Refuse protected customer data access if the app does not have the required customer data scopes and approval posture.
- Use [references/scopes.md](references/scopes.md) to map a task to the minimum viable scopes.

## Protected Operations

Require an explicit preview or change summary before execution for:

- product bulk edits
- inventory corrections
- redirects and navigation changes
- discount publication
- pixel installation
- market configuration
- translations publication
- theme writes
- checkout branding writes

For each of these, produce:

- intended change
- impacted resources
- scopes required
- rollback strategy

## API Selection

- Prefer GraphQL Admin API for new work.
- Use REST only when the host codebase already relies on REST or the GraphQL equivalent is not yet wired in the project.
- Use ShopifyQL only when `read_reports` is present.
- Use webhooks for asynchronous store events and polling only when no event source exists.

## Deterministic Helpers

Use these helpers first before inventing ad hoc code:

- `scripts/check_granted_scopes.py` to compare granted scopes against capability bundles
- `scripts/verify_shopify_hmac.py` for webhook authenticity checks
- `scripts/normalize_shopify_webhook.py` for fulfillment-mirror payload normalization
- `scripts/shopify_admin_ops.py` for deterministic Shopify Admin reads and writes in OpenClaw, with runtime token generation from `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`
- `scripts/carrier_rate_clients.py` for carrier API quote calls (UPS rating API, Chronopost quickCost SOAP, Colissimo product lookup)
- `scripts/plan_carrier_shipment.py` to aggregate order weight and dimensions, choose a package, infer colis type, and estimate checkout prices from `assets/manual-rate-policy.json`, optionally with live provider quotes
- `scripts/sendcloud_ops.py` for Sendcloud integration (shipping methods listing, parcel creation, label request from Shopify order)
- `scripts/sendcloud_webhook_receiver.py` for receiving and verifying Sendcloud webhook feedback (signature + event log)
- `scripts/attach_external_tracking.py` to create the Shopify fulfillment and attach tracking after buying a label outside Shopify
- `scripts/sync_manual_shipping_rates.py` to keep manual Shopify checkout rates aligned with transport costs and margin rules when CCS is unavailable
- `scripts/generate_sendcloud_shipping_profile.py` to derive a multi-zone Shopify delivery profile and a Sendcloud routing policy from a Sendcloud price export
- `references/carrier-packaging.md` for weight and parcel-dimension normalization rules used by Openclaw logistics and carrier-routing workflows
- `references/carrier-dispatch.md` for package-catalog planning, shipment aggregation, and label-injection prerequisites
- `references/manual-rates-without-ccs.md` for transporter-by-country checkout strategy with manual rates

## Tool Selection Rule

Choose tools in this order:

1. `check_granted_scopes.py`
   Use before implementing or claiming a capability.

2. Dedicated command in `shopify_admin_ops.py`
   Use when a command already models the task cleanly.

3. `graphql-query`
   Use for any read that is supported by the granted scopes but does not have a dedicated wrapper.

4. `graphql-mutation`
   Use for any write that is supported by the granted scopes but does not have a dedicated wrapper.

5. Custom patch to `shopify_admin_ops.py`
   Add or fix the helper only when the operation is common, repetitive, or error-prone enough to justify a stable command.

### `shopify_admin_ops.py` command families

- `context`
- `ping`
- `shop-info`
- `orders-list`
- `order-get`
- `order-update`
- `order-mark-paid`
- `fulfillment-orders-for-order`
- `fulfillment-create`
- `products-search`
- `products-by-sku`
- `product-get`
- `product-update`
- `inventory-by-sku`
- `inventory-adjust`
- `variant-logistics-get`
- `variant-logistics-set`
- `customer-get`
- `customer-update`
- `markets-list`
- `market-get`
- `market-create`
- `market-countries-add`
- `market-countries-remove`
- `delivery-profiles-list`
- `variants-shippable-list`
- `delivery-profile-assign-variants`
- `graphql-query`
- `graphql-mutation`

These command families do not define the boundary of the skill. They are the stable wrappers currently implemented.
The real boundary is: everything Shopify Admin API makes possible with the app's granted scopes, subject to protected-operation rules.

Execution patterns:

```bash
python scripts/check_granted_scopes.py --scopes "read_orders,write_orders,read_products" --capability commerce-ops
python scripts/shopify_admin_ops.py context
python scripts/shopify_admin_ops.py ping
python scripts/shopify_admin_ops.py orders-list --query "financial_status:paid"
python scripts/shopify_admin_ops.py order-get --order-name "#1004"
python scripts/shopify_admin_ops.py order-update --order-name "#1004" --note "Traitee par VD Manager" --dry-run
python scripts/shopify_admin_ops.py fulfillment-orders-for-order --order-name "#1004"
python scripts/shopify_admin_ops.py fulfillment-create --input-json "{...}" --dry-run
python scripts/shopify_admin_ops.py product-update --handle "vanille-12-gousses" --title "Vanille 12 gousses premium" --dry-run
python scripts/shopify_admin_ops.py inventory-adjust --input-json "{...}" --dry-run
python scripts/shopify_admin_ops.py markets-list
python scripts/shopify_admin_ops.py market-get --market-name "International"
python scripts/shopify_admin_ops.py market-create --name "Suisse" --country-code CH --dry-run
python scripts/shopify_admin_ops.py market-countries-add --market-name "International" --country-code AD --dry-run
python scripts/shopify_admin_ops.py market-countries-remove --market-name "International" --country-code AD --dry-run
python scripts/shopify_admin_ops.py delivery-profiles-list
python scripts/shopify_admin_ops.py variants-shippable-list --profile-name "Openclaw Shipping"
python scripts/shopify_admin_ops.py delivery-profile-assign-variants --profile-name "Openclaw Shipping" --dry-run
python scripts/shopify_admin_ops.py graphql-mutation --query-file mutation.graphql --variables-json "{...}" --dry-run
python scripts/plan_carrier_shipment.py --order-name "#1004" --packages-file assets/package-catalog.example.json
python scripts/plan_carrier_shipment.py --order-name "#1004" --packages-file assets/package-catalog.example.json --rates-policy-file assets/manual-rate-policy.json
python scripts/plan_carrier_shipment.py --order-name "#1004" --rates-policy-file assets/manual-rate-policy.json --rate-source auto
python scripts/plan_carrier_shipment.py --order-name "#1004" --rates-policy-file assets/manual-rate-policy.json --rate-source live --strict-live-rates
python scripts/sendcloud_ops.py context
python scripts/sendcloud_ops.py shipping-methods-list --from-country FR --to-country FR --weight 0.25
python scripts/sendcloud_ops.py parcel-create-from-order --order-name "#1004" --packages-file assets/package-catalog.sendcloud.json --dry-run
python scripts/sendcloud_ops.py parcel-create-from-order --order-name "#1004" --packages-file assets/package-catalog.sendcloud.json --allow-oversize-package --dry-run
python scripts/sendcloud_ops.py parcel-create-from-order --order-name "#1004" --packages-file assets/package-catalog.sendcloud.json
python scripts/sendcloud_webhook_receiver.py --signature-key "<YOUR_SIGNATURE_KEY>"
python scripts/attach_external_tracking.py --order-name "#1004" --carrier "Colissimo" --tracking-number "8X12345678901" --tracking-url "https://www.laposte.fr/outils/suivre-vos-envois?code=8X12345678901" --dry-run
python scripts/attach_external_tracking.py --order-name "#1004" --carrier "Colissimo" --tracking-number "8X12345678901" --tracking-url "https://www.laposte.fr/outils/suivre-vos-envois?code=8X12345678901"
python scripts/generate_sendcloud_shipping_profile.py --csv-file ~/Downloads/sendcloud_price_list_20260311_190656.csv
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.json
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.json --apply
```

## Resources

- Read [references/scopes.md](references/scopes.md) before implementing or expanding access.
- Read [references/capabilities.md](references/capabilities.md) to choose the right execution path.
- Read [references/shopify-api-notes.md](references/shopify-api-notes.md) for current API constraints and protected operations.
- Read [references/integration-contract.md](references/integration-contract.md) when the task still touches fulfillment intake.
- Read [references/surface-matrix.md](references/surface-matrix.md) to route tasks to dedicated commands versus raw GraphQL.
- Read [references/carrier-packaging.md](references/carrier-packaging.md) when the task needs a shipping-weight estimate, package dimensions, or carrier-ready product normalization.
- Read [references/carrier-dispatch.md](references/carrier-dispatch.md) when the task needs package selection, order-level shipment planning, or automatic tracking injection prerequisites.
- Read [references/manual-rates-without-ccs.md](references/manual-rates-without-ccs.md) when checkout must expose transporter choices on a non-CCS plan.

## Example Requests

- "Récupère les 20 dernières commandes Shopify et dis-moi lesquelles ne sont pas expédiées."
- "Analyse mon taux de conversion Shopify depuis le lancement et dis-moi quels scopes il manque."
- "Prépare un plan SEO pour les pages produits et applique les metas si les scopes content sont présents."
- "Crée un audit des permissions Shopify nécessaires pour que Openclaw gère marketing, design et catalogue."
- "Prépare une mise à jour de navigation et de redirects avec preview avant publication."
- "Modifie le branding checkout ou le thème seulement après avoir montré le diff."
- "Si aucun helper n'existe pour cette tâche Shopify, utilise directement GraphQL si les scopes le permettent."
