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

`assets/manual-rate-policy.example.json` is the baseline structure:

- `zone_name`: must match the Shopify zone name exactly.
- `service_code`: internal code used for cost lookup.
- `method_name`: customer-facing checkout label.
- `base_cost`: fallback cost when no external quote is provided.
- `margin_percent`, `margin_fixed`, `min_price`, `max_price`.

### Optional costs file

`assets/boxtal-costs.example.json` can override `base_cost` by `(zone_name, service_code)`.

## Sync Commands

Dry run:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.example.json
```

Dry run with external costs:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.example.json --costs-file assets/boxtal-costs.example.json
```

Apply:

```bash
python scripts/sync_manual_shipping_rates.py --policy-file assets/manual-rate-policy.example.json --costs-file assets/boxtal-costs.example.json --apply
```

## Notes

- This flow does not require CCS.
- Zone names must match Shopify exactly (`France`, `UE (Union Européenne)`, `International`, etc.).
- Keep the old fallback method only if intentionally needed.
- If `--delete-unmanaged` is used, unmanaged methods are listed for cleanup in the report. The current script does not auto-delete methods yet.
