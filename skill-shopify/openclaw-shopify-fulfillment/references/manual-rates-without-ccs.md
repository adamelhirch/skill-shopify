# Manual Rates Without CCS

Use this flow when Shopify live carrier rates are unavailable on the current plan.

## Objective

Let the customer choose between multiple transport services at checkout while keeping manual rates aligned with shipping costs and margin targets.

## Approach

1. Keep one shipping method per transport service in each Shopify shipping zone.
2. Recompute each method price from cost + margin.
3. Sync rates to Shopify with `scripts/sync_manual_shipping_rates.py`.

## Inputs

### Policy file

`assets/manual-rate-policy.json` is the main source of truth:

- `default_margin_percent`: global margin percent, validated between `0` and `100`.
- `default_margin_fixed`, `default_min_price`, `default_max_price`: global pricing controls.
- `features`: explicit toggles for unsupported checkout surfaces on Basic plans.
- `colis_types` and `active_colis_type`: package constants used to select service cost tiers.
- legacy mode:
  - `zones`: hard-coded mapping `zone_name -> services` for one target profile
- multi-profile mode:
  - `profiles`: array of profile policies
  - each profile can define `profile_name` or `profile_id`
  - optional `create_if_missing`: create the Shopify delivery profile when it does not exist yet
  - optional `location_ids`: Shopify location ids to attach when creating a missing profile
  - each profile keeps its own `zones`
- optional `country_codes`: ISO alpha-2 list used to auto-create the Shopify zone if it does not exist yet.
- each service defines `method_name` and either:
  - `base_cost`
  - or `base_cost_by_colis_type` (recommended)
- optional `description`: copied to the Shopify shipping method description
- optional carrier metadata can be stored in:
  - `carrier_name`
  - `carrier_service_name`
  - `carrier_service_code`
  - `carrier_service_api_code` (optional override when provider API expects a different code than checkout display code)

The sync script resolves cost in this order:

1. `base_cost_by_colis_type[active_colis_type]`
2. fallback `base_cost`

If carrier costs change, Openclaw must update `assets/manual-rate-policy.json` first, then run sync.

`zone_name` must match the Shopify zone label exactly.

## Sync Commands

Generate the policy and Sendcloud routing map from a Sendcloud CSV export:

```bash
python scripts/generate_sendcloud_shipping_profile.py --csv-file ~/Downloads/sendcloud_price_list_20260311_190656.csv
```

This generates:

- `assets/manual-rate-policy.json`
- `assets/sendcloud-routing-policy.json`

Dry run:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.json
```

Dry run for one specific profile:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.json --profile-name "General profile"
```

Apply:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.json --apply
```

## Notes

- This flow does not require CCS.
- This flow is compatible with Shopify Basic when pickup-point checkout and live carrier rates are disabled.
- After buying a label outside Shopify, attach tracking with `scripts/attach_external_tracking.py`.
- Sendcloud routing is resolved explicitly by `scripts/sendcloud_ops.py` through `assets/sendcloud-routing-policy.json`; this avoids relying on non-public Sendcloud shipping-rules CRUD.
- If `country_codes` is omitted, zone names must match Shopify exactly (`France`, `UE (Union Européenne)`, `International`, etc.).
- If `country_codes` is present and the zone does not exist yet, the sync script can create the Shopify zone and its rates in one pass.
- If `profiles[]` is used and `create_if_missing` is true, the sync script can also create the Shopify delivery profile itself before attaching zones and methods.
- If `--delete-unmanaged` is enabled, unmanaged methods are removed from the profile update payload.
