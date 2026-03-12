import argparse

from shopify_admin_ops import fail, graph_ql, output, resolve_context, resolve_order_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Shopify fulfillment with external tracking after buying a label outside Shopify."
    )
    parser.add_argument("--store")
    parser.add_argument("--shop-url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")
    parser.add_argument("--api-version")
    parser.add_argument("--order-id")
    parser.add_argument("--order-name")
    parser.add_argument("--carrier", required=True)
    parser.add_argument("--tracking-number", required=True)
    parser.add_argument("--tracking-url")
    parser.add_argument("--message")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def get_open_fulfillment_orders(context: dict, order_gid: str) -> tuple[dict, list[dict]]:
    data = graph_ql(
        context,
        """
        query ExternalTrackingFulfillmentOrders($id: ID!) {
          order(id: $id) {
            id
            name
            fulfillmentOrders(first: 50) {
              nodes {
                id
                status
                requestStatus
                lineItems(first: 50) {
                  nodes {
                    id
                    remainingQuantity
                    totalQuantity
                    lineItem {
                      name
                      sku
                    }
                  }
                }
              }
            }
          }
        }
        """,
        {"id": order_gid},
    )
    order = data.get("order")
    if not order:
        fail("Order not found")

    selected = []
    for fulfillment_order in (order.get("fulfillmentOrders") or {}).get("nodes") or []:
        line_items = []
        for line in (fulfillment_order.get("lineItems") or {}).get("nodes") or []:
            remaining_quantity = int(line.get("remainingQuantity") or 0)
            if remaining_quantity <= 0:
                continue
            line_items.append(
                {
                    "id": line.get("id"),
                    "remaining_quantity": remaining_quantity,
                    "total_quantity": int(line.get("totalQuantity") or 0),
                    "name": ((line.get("lineItem") or {}).get("name")),
                    "sku": ((line.get("lineItem") or {}).get("sku")),
                }
            )
        if not line_items:
            continue
        selected.append(
            {
                "id": fulfillment_order.get("id"),
                "status": fulfillment_order.get("status"),
                "request_status": fulfillment_order.get("requestStatus"),
                "line_items": line_items,
            }
        )
    return order, selected


def build_fulfillment_input(args: argparse.Namespace, fulfillment_orders: list[dict]) -> dict:
    line_items_by_fulfillment_order = []
    for fulfillment_order in fulfillment_orders:
        line_items_by_fulfillment_order.append(
            {
                "fulfillmentOrderId": fulfillment_order["id"],
                "fulfillmentOrderLineItems": [
                    {
                        "id": line_item["id"],
                        "quantity": line_item["remaining_quantity"],
                    }
                    for line_item in fulfillment_order["line_items"]
                ],
            }
        )

    tracking_info = {
        "company": args.carrier,
        "number": args.tracking_number,
    }
    if args.tracking_url:
        tracking_info["url"] = args.tracking_url

    return {
        "lineItemsByFulfillmentOrder": line_items_by_fulfillment_order,
        "trackingInfo": tracking_info,
    }


def fulfillment_create(context: dict, fulfillment_input: dict, message: str | None) -> dict:
    return graph_ql(
        context,
        """
        mutation ExternalTrackingFulfillmentCreate($fulfillment: FulfillmentInput!, $message: String) {
          fulfillmentCreate(fulfillment: $fulfillment, message: $message) {
            fulfillment {
              id
              status
              trackingInfo(first: 10) {
                company
                number
                url
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """,
        {"fulfillment": fulfillment_input, "message": message},
    )


def main() -> None:
    args = parse_args()
    context = resolve_context(args)
    order_gid = resolve_order_id(context, args.order_id, args.order_name)
    order, open_fulfillment_orders = get_open_fulfillment_orders(context, order_gid)
    if not open_fulfillment_orders:
        fail("No fulfillable line item found (remainingQuantity is 0 for all fulfillment orders)")

    fulfillment_input = build_fulfillment_input(args, open_fulfillment_orders)
    if args.dry_run:
        output(
            {
                "ok": True,
                "mode": "attach-external-tracking",
                "dry_run": True,
                "store_domain": context["store_domain"],
                "order": {"id": order.get("id"), "name": order.get("name")},
                "selected_fulfillment_orders": open_fulfillment_orders,
                "fulfillment_input": fulfillment_input,
                "message": args.message,
            }
        )
        return

    result = fulfillment_create(context, fulfillment_input, args.message)
    output(
        {
            "ok": True,
            "mode": "attach-external-tracking",
            "dry_run": False,
            "store_domain": context["store_domain"],
            "order": {"id": order.get("id"), "name": order.get("name")},
            "selected_fulfillment_orders": open_fulfillment_orders,
            "fulfillment_input": fulfillment_input,
            "result": result,
        }
    )


if __name__ == "__main__":
    main()
