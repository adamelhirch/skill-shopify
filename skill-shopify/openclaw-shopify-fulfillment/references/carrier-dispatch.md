# Carrier Dispatch Flow

Use this reference when Openclaw must choose a parcel and prepare an external carrier label workflow.

## Goal

Openclaw should be able to:

1. read the Shopify order
2. compute shipment weight from the sum of variant shipping weights
3. compute aggregate parcel dimensions from variant logistics metafields
4. choose the smallest compatible package from a known package catalog
5. estimate customer-facing rate by zone/service from `assets/manual-rate-policy.json`
6. inject tracking into Shopify once an external label has been purchased

## Required Inputs

### Shopify catalog

Per shippable variant:

- native Shopify shipping weight on the inventory item
- `openclaw_logistics.packaging_type`
- `openclaw_logistics.net_weight_kg`
- `openclaw_logistics.parcel_length_cm`
- `openclaw_logistics.parcel_width_cm`
- `openclaw_logistics.parcel_height_cm`

### Openclaw package catalog

Each available parcel format must have:

- `code`
- `label`
- `inner_length_cm`
- `inner_width_cm`
- `inner_height_cm`
- `empty_weight_kg`
- `max_weight_kg`
- `enabled`

See [assets/package-catalog.example.json](../assets/package-catalog.example.json).

## Execution Modes

### Mode A: planning only

Use `scripts/plan_carrier_shipment.py`.

This mode is enough to:

- validate data completeness
- estimate the selected package
- produce a final shipment weight
- estimate checkout shipping rates for configured services
- build a carrier-ready payload

Live provider quotes are now supported in the same command:

- `--rate-source policy`: never call carrier APIs, use policy costs only
- `--rate-source auto`: try carrier APIs, fallback to policy costs if quote fails
- `--rate-source live`: require carrier API quote success (with `--strict-live-rates`)

Example:

```bash
python scripts/plan_carrier_shipment.py --order-name "#1004" --rates-policy-file assets/manual-rate-policy.json --rate-source auto
python scripts/plan_carrier_shipment.py --order-name "#1004" --rates-policy-file assets/manual-rate-policy.json --rate-source live --strict-live-rates
```

Carrier credentials expected in environment:

- UPS: `UPS_CLIENT_ID`, `UPS_CLIENT_SECRET`, `UPS_ACCOUNT_NUMBER`, `UPS_SHIPPER_POSTAL_CODE`
- Chronopost: `CHRONOPOST_ACCOUNT_NUMBER`, `CHRONOPOST_PASSWORD`, `CHRONOPOST_SHIPPER_POSTAL_CODE`
- Colissimo: `COLISSIMO_CONTRACT_NUMBER`, `COLISSIMO_PASSWORD`

Note: Colissimo SLS endpoint is wired for service lookup, but does not expose a straightforward quote amount in this flow. The script falls back to policy cost for pricing.

### Mode B: external carrier label purchase

If labels are bought outside Shopify (Colissimo, Chronopost, UPS portal/API):

- transform `carrier_ready_payload` into the target carrier payload
- buy label in carrier system
- store returned tracking number and tracking URL in Openclaw
- inject tracking in Shopify via `scripts/attach_external_tracking.py`

### Mode B2: Sendcloud label purchase (recommended for your current setup)

If Sendcloud app is installed and linked to the store:

- list shipping methods with `scripts/sendcloud_ops.py shipping-methods-list`
- create parcel+label directly from Shopify order with `scripts/sendcloud_ops.py parcel-create-from-order`
- default package catalog can start with only one box (`20x15x5`, empty weight `0.02kg`) in `assets/package-catalog.sendcloud.json`
- inject tracking in Shopify after label creation via `scripts/attach_external_tracking.py`
- if an order does not fit current package list, use `--allow-oversize-package` temporarily and then add a larger package in catalog

Environment variables used by `sendcloud_ops.py`:

- `SENDCLOUD_PUBLIC_KEY`
- `SENDCLOUD_SECRET_KEY`
- optional `SENDCLOUD_AUTH_MODE` (`auto`, `basic`, `oauth2`)
- optional `SENDCLOUD_API_BASE_URL` (default `https://panel.sendcloud.sc`)
- optional `SENDCLOUD_TOKEN_URL` (default `https://account.sendcloud.com/oauth2/token`)

Webhook feedback setup (Sendcloud Integration UI):

1. Enable `Webhook feedback enabled`.
2. Set `Webhook Signature Key` to a strong random secret.
3. Set `Webhook url` to your public endpoint, for example:
   - `https://<your-domain>/webhooks/sendcloud`
4. Keep `Feedback to the webshop` on the delayed scan mode if you want status updates only after first carrier scan.

Local receiver for testing:

```bash
python scripts/sendcloud_webhook_receiver.py --signature-key "<YOUR_SIGNATURE_KEY>"
```

The receiver exposes:
- webhook endpoint: `/webhooks/sendcloud`
- health endpoint: `/healthz`
- JSONL events log: `tmp/sendcloud-webhook-events.jsonl`

### Mode C: manual checkout rates without CCS

If the store plan cannot expose live carrier rates:

- keep one checkout method per transport service and zone
- compute fixed prices from `base_cost` or `base_cost_by_colis_type` + margin
- sync methods with `scripts/sync_manual_shipping_rates.py`
- see [manual-rates-without-ccs.md](manual-rates-without-ccs.md)
