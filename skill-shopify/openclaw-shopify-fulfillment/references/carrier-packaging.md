# Carrier Packaging Playbook

Use this reference when Openclaw must estimate or normalize shipping weight and parcel dimensions for Shopify products before carrier label purchase.

## Principles

- Distinguish `net_weight_kg` from the Shopify shipping weight stored on the inventory item.
- The Shopify shipping weight should include the primary container and a modest packing allowance when the product ships alone.
- Store parcel dimensions in Openclaw-owned metafields when Shopify doesn't expose a direct per-variant length, width, and height field.

Namespace used by the skill:

- `openclaw_logistics.packaging_type`
- `openclaw_logistics.net_weight_kg`
- `openclaw_logistics.parcel_length_cm`
- `openclaw_logistics.parcel_width_cm`
- `openclaw_logistics.parcel_height_cm`

## Suggested Class Logic

### Powdered spices in shaker pot

- Typical examples: `pot a epices 40g`
- Container type: plastic or light shaker pot
- Net fill: `0.04 kg`
- Shipping weight target: `0.09 kg` to `0.11 kg`
- Solo parcel footprint: `6 x 6 x 11 cm`

### Powdered spices in glass jar

- Typical examples: `pot en verre 40g`
- Container type: glass jar
- Net fill: `0.04 kg`
- Shipping weight target: `0.18 kg` to `0.24 kg`
- Solo parcel footprint: `6 x 6 x 11 cm`

### Sachet spices 25g to 50g

- Typical examples: `sachet 30g`, `sachet 40g`, `sachet 50g`
- Container type: flat pouch
- Net fill follows title quantity
- Shipping weight target: net plus `0.01 kg` to `0.03 kg`
- Solo parcel footprint: `16 x 11 x 2 cm`

### Vanilla tubes

- Typical examples: `tube en verre`, `tube`
- Container type: rigid tube
- Shipping weight target depends on gousse count and tube material
- Start with `0.06 kg` to `0.16 kg`
- Solo parcel footprint: `22 x 4 x 4 cm`

### Oils and extracts in small bottle

- Typical examples: `20ml`, `5mL`, `100mL`
- Container type: glass or PET bottle
- Shipping weight target includes bottle weight
- Solo parcel footprint:
  - `5 mL`: `4 x 4 x 8 cm`
  - `20 mL`: `4 x 4 x 11 cm`
  - `100 mL`: `5 x 5 x 15 cm`

## Operating Rule

For the first pass:

1. infer the product class from title, description, and variant title
2. assign `weight_kg` on the inventory item
3. persist the estimated dimensions and container type in `openclaw_logistics` metafields
4. only map to a Shopify shipping package id later, once the store's package catalog is stabilized
