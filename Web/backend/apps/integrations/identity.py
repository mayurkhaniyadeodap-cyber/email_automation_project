"""
Identity & contact resolution / self-lookup (DeoDap Care — Final Mail Flow §3b, §6).

Before the Mail Engine asks the customer for anything, it tries to find their order
from what's already in the mail: the order number, or -- failing that -- the sender's
email or phone as a search key against Shopify.

    * exactly 1 recent order  -> auto-select it (no question asked)
    * 2+ recent orders        -> needs_choice (ask which one, via M1)
    * nothing                 -> no order resolved (the caller may send M1)

Config-gated: with no Shopify client configured the lookup returns
`configured=False` and the caller leaves the existing flow untouched (it never
sends M1 it can't substantiate).
"""

import logging

logger = logging.getLogger(__name__)


def _empty(configured, source="none"):
    return {"order": None, "orders": [], "needs_choice": False,
            "source": source, "configured": configured}


def _select(orders, source, configured):
    if not orders:
        return None
    if len(orders) == 1:
        return {"order": orders[0], "orders": orders, "needs_choice": False,
                "source": source, "configured": configured}
    return {"order": None, "orders": orders, "needs_choice": True,
            "source": source, "configured": configured}


def resolve_identity(brand, message, *, extracted=None, clients=None):
    """Self-lookup the customer's order(s). Returns a dict (see module docstring).

    `extracted` (optional) supplies already-parsed order_id/phone; otherwise we parse
    the subject + body. Never raises -- a lookup failure resolves to "nothing found".
    """
    from apps.classifier.rule_classifier import _extract_order_id, _extract_phone

    extracted = extracted or {}
    text = f"{message.get('subject', '')} {message.get('body_text', '')}"
    order_id = extracted.get("order_id") or _extract_order_id(text)
    phone = extracted.get("phone") or _extract_phone(text)
    email = (message.get("from_email") or "").strip()

    if clients is None:
        from apps.integrations import context

        clients = context.build_clients(context._settings_for(brand))
    shopify = clients.get("shopify")
    if shopify is None:
        return _empty(configured=False)

    # 1) Order number is the strongest key.
    if order_id:
        try:
            order = shopify.get_order(order_id)
            if order:
                return {"order": order, "orders": [order], "needs_choice": False,
                        "source": "order_id", "configured": True}
        except Exception:  # noqa: BLE001
            logger.exception("Shopify get_order failed for %s", order_id)

    # 2) Sender email as a search key.
    if email:
        try:
            res = _select(shopify.recent_orders_by_email(email) or [], "email", True)
            if res:
                return res
        except Exception:  # noqa: BLE001
            logger.exception("Shopify recent_orders_by_email failed for %s", email)

    # 3) Phone as a search key.
    if phone:
        try:
            res = _select(shopify.recent_orders_by_phone(phone) or [], "phone", True)
            if res:
                return res
        except Exception:  # noqa: BLE001
            logger.exception("Shopify recent_orders_by_phone failed for %s", phone)

    return _empty(configured=True)
