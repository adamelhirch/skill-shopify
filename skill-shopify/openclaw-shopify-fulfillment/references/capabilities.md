# Capability Playbook

Use this file to decide how Openclaw should execute a Shopify task.

## Capability Decision Tree

### Orders, stock, shipping, returns

Choose `commerce-ops`.

- Read order and fulfillment state first.
- Use background jobs for syncs, exports, and webhook-driven workflows.
- Keep idempotency keys for every asynchronous event.

### Products, collections, inventory content, media

Choose `catalog-ops`.

- Prefer GraphQL bulk reads when the catalog is large.
- Produce a diff before bulk writes.
- Use files, metafields, and metaobjects for structured enrichment rather than overloading product descriptions.

### Pages, blogs, redirects, menus, translations, locales

Choose `seo-content`.

- Generate the editorial and SEO plan before changing store data.
- Keep publishing and strategy distinct. Openclaw should be able to propose without publishing.
- Use redirects and navigation changes only with an explicit impact summary.

### Traffic analysis, conversion, reporting, discounts, pixels

Choose `marketing-analytics`.

- Verify `read_reports` before any traffic or conversion claim.
- Separate measurement from execution.
- Use customer events and pixels for instrumentation, then reports for analysis.

### Theme edits, checkout look and feel, visual hierarchy

Choose `design-storefront`.

- Start with the least invasive option.
- Use theme reads for audit, not just for writing.
- Publish only after diff review.

### Customers, support, lifecycle segmentation

Choose `customer-crm`.

- Minimize access to protected fields.
- Do not read protected customer data unless explicitly required and permitted.

## Openclaw Operating Pattern

For each capability:

1. Discover current state from Shopify.
2. Build a normalized Openclaw view.
3. Compute the proposed action or report.
4. Gate writes with a preview when risk is non-trivial.
5. Execute with audit logging and rollback notes.

## Reporting Rules

When the user asks for performance evolution:

- If `read_reports` is granted, use analytics and say it is a true traffic-based report.
- If only order scopes are granted, label the output as an order-performance report, not a conversion report.
- Always state the scope-based limitation explicitly.

## Design Rules

When the user asks to redesign the store:

- Audit the current theme, content, navigation, and branding settings first.
- Prefer incremental, testable changes over a full rewrite.
- Preserve brand consistency if existing assets or guidelines exist.

## SEO Rules

When the user asks for SEO work:

- Inspect products, collections, pages, and blogs.
- Identify thin content, missing metas, duplicate patterns, and navigation gaps.
- Propose structured changes first.
- Apply content updates only when the relevant write scopes are present.
