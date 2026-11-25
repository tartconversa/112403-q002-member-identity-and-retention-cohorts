"""Order state and refund correction logic."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import Order, ValidOrder

CANCEL_STATES = {"cancelled", "canceled", "cancel", "void", "voided", "已取消", "取消", "作废"}
FULL_REFUND_STATES = {
    "refunded",
    "fully_refunded",
    "full_refunded",
    "refund_full",
    "returned_full",
    "已退款",
    "全额退款",
    "退款完成",
}
PARTIAL_REFUND_STATES = {"partially_refunded", "partial_refunded", "refund_partial", "部分退款", "部分退货"}
COMPLETED_STATES = {"completed", "complete", "settled", "paid", "已完成", "完成", "交易成功"}


def canonical_order_state(state: str) -> str:
    value = (state or "").lower()
    if value in CANCEL_STATES:
        return "cancelled"
    if value in FULL_REFUND_STATES:
        return "fully_refunded"
    if value in PARTIAL_REFUND_STATES:
        return "partially_refunded"
    if value in COMPLETED_STATES:
        return "completed"
    return value or "unknown"


def build_valid_orders(orders: list[Order], refunds_by_order: dict[str, list] | list | None = None) -> dict[str, ValidOrder]:
    """Apply cancellations and known refunds.

    ``refunds_by_order`` may be a flat list of Refund objects (the internal
    calling convention) or a mapping of order_id to refunds.
    """
    grouped_refunds: dict[str, list] = defaultdict(list)
    if isinstance(refunds_by_order, dict):
        refund_iterable = [refund for items in refunds_by_order.values() for refund in items]
    else:
        refund_iterable = refunds_by_order or []
    for refund in refund_iterable:
        grouped_refunds[refund.order_id].append(refund)

    result: dict[str, ValidOrder] = {}
    for order in orders:
        itemized_total = sum((r.amount for r in grouped_refunds.get(order.order_id, [])), Decimal("0"))
        source_refund_total = order.refunded_amount or Decimal("0")
        # The column may be a cumulative refund position while refunds.csv may
        # contain newer events.  Taking the larger known position avoids double
        # counting an exported cumulative amount and its itemized refund rows.
        refund_total = max(itemized_total, source_refund_total)
        state = canonical_order_state(order.state)
        net = order.paid - refund_total
        fully_refunded = (
            state == "fully_refunded"
            or (order.paid > 0 and refund_total >= order.paid)
            or (refund_total > 0 and net <= 0)
        )
        if state == "cancelled":
            effective = "cancelled"
        elif fully_refunded:
            effective = "fully_refunded"
            net = Decimal("0")
            if refund_total <= 0:
                # The terminal state itself asserts a full refund even when
                # the source has no separate refund event/amount column.
                refund_total = order.paid
        elif state not in COMPLETED_STATES and state != "partially_refunded":
            effective = state
        elif net <= 0:
            effective = "zero_or_negative"
        else:
            effective = "valid"
        result[order.order_id] = ValidOrder(
            order=order,
            net_amount=max(net, Decimal("0")),
            fully_refunded=fully_refunded,
            refund_total=refund_total,
            effective_state=effective,
        )
    return result


def is_effectively_valid(record: ValidOrder) -> bool:
    return record.effective_state == "valid" and record.net_amount > 0
