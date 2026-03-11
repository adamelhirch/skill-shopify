# Shopify Surface Matrix

Use this file to decide whether an agent should rely on an existing helper or go directly through raw GraphQL.

## Core Principle

The skill covers every Shopify Admin capability that is actually reachable with the app's granted scopes.
Dedicated helpers are convenience wrappers, not the limit of the skill.

## Existing Wrapped Surfaces

These already have stable commands in `scripts/shopify_admin_ops.py`:

- shop profile
- orders read and simple order writes
- fulfillments creation
- product reads and simple product writes
- inventory lookup and inventory adjustment
- variant logistics reads and writes for shipping weight plus Openclaw parcel metafields
- customer reads and customer updates
- raw GraphQL query execution
- raw GraphQL mutation execution

## Raw GraphQL Surfaces

Use `graphql-query` or `graphql-mutation` when the task touches any of these and no dedicated helper exists:

- discounts
- price rules
- reports and ShopifyQL-adjacent reporting entry points
- metaobjects and metaobject definitions
- content and online store pages
- navigation
- translations and locales
- markets
- checkout branding
- themes
- pixels
- product feeds
- publications
- order edits
- draft orders
- returns
- legal policies
- delivery customizations
- app proxy configuration
- validation functions
- cart transforms
- Shopify Payments read surfaces
- any future Shopify Admin surface exposed by the granted scopes

## Routing Rules

### Reads

- If a helper exists and returns the needed fields, use it.
- If the helper is missing fields or lacks the right flags, use `graphql-query`.
- If the request is exploratory, start with a narrow query, then widen it.

### Writes

- If a helper already models the mutation safely, use it.
- If no helper exists, use `graphql-mutation` with explicit variables.
- For non-trivial writes, produce a preview of the intended payload first.
- For high-impact writes, include rollback notes in the response.

## Patch Triggers

Patch `shopify_admin_ops.py` when one of these is true:

- the same GraphQL pattern is likely to be reused often
- the mutation payload is easy to get wrong
- a dry-run or guardrail is needed
- the user repeatedly asks for the same class of task

## Do Not Artificially Limit Capability

Do not tell the user "the skill cannot do this" just because there is no dedicated helper command yet.
Instead:

1. check the granted scopes
2. check the relevant Shopify Admin object or mutation
3. run the operation through raw GraphQL if the scopes allow it
4. only refuse if Shopify itself rejects the capability or if the operation violates the protected-operation rules
