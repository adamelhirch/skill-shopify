# Boxtal Dispatch Flow

Use this reference when Openclaw must choose a parcel and prepare a carrier label workflow.

## Goal

Openclaw should be able to:

1. read the Shopify order
2. compute shipment weight from the sum of variant shipping weights
3. compute aggregate parcel dimensions from variant logistics metafields
4. choose the smallest compatible package from a known package catalog
5. produce a carrier-ready payload
6. trigger label creation once the Boxtal execution surface is configured

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

Use `scripts/plan_boxtal_shipment.py`.

This mode is enough to:

- validate data completeness
- estimate the selected package
- produce a final shipment weight
- build a carrier-ready payload

### Mode B: import-based dispatch

If Boxtal is operated through CSV or spreadsheet import rather than API:

- transform the `boxtal_ready_payload` into the exact import schema expected by the workspace
- attach the generated file to the operator workflow

### Mode B2: manual checkout rates without CCS

If the store plan cannot expose live carrier rates:

- keep one checkout method per transport service and zone
- compute fixed prices from cost + margin
- sync methods with `scripts/sync_manual_shipping_rates.py`
- see [manual-rates-without-ccs.md](manual-rates-without-ccs.md)

### Mode C: API or browser execution

If the business later provides a Boxtal API integration or an approved browser automation path:

- reuse the same package selection logic
- pass the selected package plus recipient payload into Boxtal
- store the returned tracking number and label URL back into Openclaw and Shopify fulfillment notes

## What Is Still Needed For Full Label Creation

To create a real shipping label automatically, Openclaw still needs one of these:

- a Boxtal API credential set and endpoint contract
- or an approved browser automation workflow against the Boxtal back office
- or an import template that Boxtal can consume without manual re-entry

Without one of those execution surfaces, Openclaw can fully plan the shipment but cannot yet buy and retrieve the label document.
