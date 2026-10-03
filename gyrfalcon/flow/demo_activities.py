"""A published, deterministic order-flow example used to exercise the visual runtime."""

from __future__ import annotations

from typing import Any

from gyrfalcon.flow.graph_context import ActivityContext, ActivityResult
from gyrfalcon.flow.templates import activity


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@activity(name="demo.customer_route", version="1", description="Route the order by customer tier.")
def customer_route(ctx: ActivityContext) -> ActivityResult:
    tier = str(ctx.inputs.get("customer_tier", "standard")).lower()
    tier = "gold" if tier == "gold" else "standard"
    order = {
        "order_id": str(ctx.inputs.get("order_id", "ORD-1042")),
        "amount": _number(ctx.inputs.get("amount", 320.0)),
        "risk_score": _number(ctx.inputs.get("risk_score", 0.82)),
        "inventory_count": int(_number(ctx.inputs.get("inventory_count", 0))),
        "shipping_method": str(ctx.inputs.get("shipping_method", "parcel")).lower(),
        "customer_tier": tier,
    }
    return ActivityResult(tier, order, {"customer_segment": tier})


@activity(name="demo.gold_discount", version="1", description="Apply the gold-tier discount and estimate tax.")
def gold_discount(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    rate = 0.10
    discounted = round(_number(order.get("amount")) * (1 - rate), 2)
    tax = round(discounted * _number(ctx.attr("tax_rate"), 0.075), 2)
    order.update(discount_rate=rate, discounted_total=discounted, estimated_tax=tax)
    return ActivityResult("done", order, {"discount_rate": rate, "discounted_total": discounted})


@activity(name="demo.standard_discount", version="1", description="Calculate the standard-tier total and tax.")
def standard_discount(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    rate = 0.02
    discounted = round(_number(order.get("amount")) * (1 - rate), 2)
    tax = round(discounted * _number(ctx.attr("tax_rate"), 0.075), 2)
    order.update(discount_rate=rate, discounted_total=discounted, estimated_tax=tax)
    return ActivityResult("done", order, {"discount_rate": rate, "discounted_total": discounted})


@activity(name="demo.risk_route", version="1", description="Choose automatic clearance or manual review.")
def risk_route(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    threshold = _number(ctx.inputs.get("manual_review_threshold", 0.7), 0.7)
    band = "review" if _number(order.get("risk_score")) >= threshold else "clear"
    order["risk_band"] = band
    return ActivityResult(band, order, {"risk_band": band})


@activity(name="demo.auto_clear", version="1", description="Record the automatic approval decision.")
def auto_clear(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    order["approval_status"] = "approved"
    return ActivityResult("done", order, {"approval_status": "approved"})


@activity(name="demo.manual_review", version="1", description="Record the demo's manual-review result.")
def manual_review(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    decision = str(ctx.inputs.get("manual_review_result", "approved")).lower()
    status = "approved" if decision == "approved" else "rejected"
    order["approval_status"] = status
    return ActivityResult("done", order, {"approval_status": status})


@activity(name="demo.inventory_route", version="1", description="Select the available-stock or procurement path.")
def inventory_route(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    stock = max(0, int(_number(order.get("inventory_count"))))
    availability = "available" if stock > 0 else "backorder"
    order["inventory_state"] = availability
    return ActivityResult(availability, order, {"inventory_state": availability})


@activity(name="demo.reserve_inventory", version="1", description="Reserve available units for the order.")
def reserve_inventory(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    units = max(1, int(_number(ctx.inputs.get("quantity", 1))))
    order["inventory_action"] = "reserved"
    order["reserved_units"] = units
    return ActivityResult("done", order, {"reserved_units": units})


@activity(name="demo.request_procurement", version="1", description="Create a deterministic demo replenishment reference.")
def request_procurement(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    order["inventory_action"] = "procurement_requested"
    order["purchase_order_ref"] = f"PO-{order.get('order_id', 'DEMO')}"
    return ActivityResult("done", order, {"purchase_order_ref": order["purchase_order_ref"]})


@activity(name="demo.shipment_route", version="1", description="Choose parcel, pickup, or hold completion.")
def shipment_route(ctx: ActivityContext) -> ActivityResult:
    order = dict(ctx.incoming_value or {})
    method = str(order.get("shipping_method", "parcel")).lower()
    method = method if method in {"parcel", "pickup", "hold"} else "parcel"
    order["shipping_method"] = method
    return ActivityResult(method, order, {"shipment_method": method})


def _attr(name: str, kind: str, default: Any, candidates: list[Any] | None = None) -> dict:
    return {"id": name, "name": name, "type": kind, "defaultValue": default,
            "candidates": candidates or []}


def sample_graph() -> dict:
    """Return ten executable Activities, four exclusive decision forks, and three Ends."""
    activities = [
        ("customer-route", "Route by customer tier", "demo.customer_route", ["gold", "standard"],
         [_attr("customer_segment", "string", "standard", ["gold", "standard"])], (170, 250)),
        ("gold-discount", "Gold discount", "demo.gold_discount", ["done"],
         [_attr("discount_rate", "number", 0.10), _attr("discounted_total", "number", 288.0)], (410, 100)),
        ("standard-discount", "Standard discount", "demo.standard_discount", ["done"],
         [_attr("discount_rate", "number", 0.02), _attr("discounted_total", "number", 313.6)], (410, 400)),
        ("risk-route", "Risk assessment", "demo.risk_route", ["clear", "review"],
         [_attr("risk_band", "string", "review", ["clear", "review"])], (650, 250)),
        ("auto-clear", "Automatic approval", "demo.auto_clear", ["done"],
         [_attr("approval_status", "string", "approved", ["approved"])], (890, 100)),
        ("manual-review", "Manual review", "demo.manual_review", ["done"],
         [_attr("approval_status", "string", "approved", ["approved", "rejected"])], (890, 400)),
        ("inventory-route", "Check inventory", "demo.inventory_route", ["available", "backorder"],
         [_attr("inventory_state", "string", "backorder", ["available", "backorder"])], (1130, 250)),
        ("reserve-stock", "Reserve stock", "demo.reserve_inventory", ["done"],
         [_attr("reserved_units", "number", 1)], (1370, 100)),
        ("procure-stock", "Request replenishment", "demo.request_procurement", ["done"],
         [_attr("purchase_order_ref", "string", "PO-ORD-1042")], (1370, 400)),
        ("shipment-route", "Choose fulfillment", "demo.shipment_route", ["parcel", "pickup", "hold"],
         [_attr("shipment_method", "string", "parcel", ["parcel", "pickup", "hold"])], (1610, 250)),
    ]
    nodes = [{"id": "start", "type": "start", "label": "Order received", "position": {"x": -70, "y": 250}, "transients": ["next"], "attributes": []}]
    for node_id, title, activity_name, transients, attrs, (x, y) in activities:
        node = {"id": node_id, "type": "python", "label": title,
                "position": {"x": x, "y": y}, "activity": activity_name, "version": "1",
                "transients": transients, "attributes": attrs}
        if activity_name in {"demo.gold_discount", "demo.standard_discount"}:
            node["flowAttributeRefs"] = ["tax-rate"]
        nodes.append(node)
    nodes.extend([
        {"id": "end-parcel", "type": "end", "label": "Parcel complete", "position": {"x": 1860, "y": 80}, "transients": ["done"], "attributes": []},
        {"id": "end-pickup", "type": "end", "label": "Pickup ready", "position": {"x": 1860, "y": 250}, "transients": ["done"], "attributes": []},
        {"id": "end-hold", "type": "end", "label": "Order held", "position": {"x": 1860, "y": 420}, "transients": ["done"], "attributes": []},
    ])
    pairs = [
        ("start", "next", "customer-route"),
        ("customer-route", "gold", "gold-discount"),
        ("customer-route", "standard", "standard-discount"),
        ("gold-discount", "done", "risk-route"),
        ("standard-discount", "done", "risk-route"),
        ("risk-route", "clear", "auto-clear"),
        ("risk-route", "review", "manual-review"),
        ("auto-clear", "done", "inventory-route"),
        ("manual-review", "done", "inventory-route"),
        ("inventory-route", "available", "reserve-stock"),
        ("inventory-route", "backorder", "procure-stock"),
        ("reserve-stock", "done", "shipment-route"),
        ("procure-stock", "done", "shipment-route"),
        ("shipment-route", "parcel", "end-parcel"),
        ("shipment-route", "pickup", "end-pickup"),
        ("shipment-route", "hold", "end-hold"),
    ]
    tax_rate = _attr("tax-rate", "number", 0.075, [0.075, 0.08])
    tax_rate["name"] = "tax_rate"
    return {"version": 2,
            "attributes": [tax_rate],
            "nodes": nodes,
            "edges": [{"id": f"e-{index:02}", "from": source,
                       "transient": transient, "to": target, "routing": "smoothstep"}
                      for index, (source, transient, target) in enumerate(pairs, 1)]}


SAMPLE_INPUTS = {
    "order_id": "ORD-1042", "amount": 320.0, "customer_tier": "gold",
    "risk_score": 0.82, "manual_review_threshold": 0.7,
    "manual_review_result": "approved", "inventory_count": 0, "quantity": 2,
    "shipping_method": "parcel",
}
