"""
Live-data integration clients (doc sections 1 & 8): Shopify (order/EDD), the
Shipping Portal (tracking/EDD), and GoKwik (payment). Each client talks to its
service and returns a NORMALIZED dict the context builder understands, or None on
any failure / missing config -- so the decision engine safely drafts instead of
auto-answering with stale data (doc section 6, "needs live data -> draft + flag").

`requests` is imported lazily so the engine and the offline test suite run without
it; tests inject fakes via context.build_context(..., clients=...).

Per-brand credentials live on BrandSettings.integrations:
    {"shopify":  {"shop": "x.myshopify.com", "token": "...", "api_version": "2024-10"},
     "shipping": {"base_url": "https://ship.example/api", "api_key": "..."},
     "gokwik":   {"base_url": "https://api.gokwik.co", "api_key": "..."}}
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8  # seconds


def _requests():
    import requests

    return requests


def _looks_like_person_name(value):
    """A real person name has letters and NO digits or address punctuation. Rejects values like
    '1/166 - 42' (a door/street number the customer typed into a name field on Shopify)."""
    s = (value or "").strip()
    if len(s) < 2 or not any(c.isalpha() for c in s):
        return False
    if any(c.isdigit() for c in s) or "/" in s:
        return False
    return True


def _last10(value):
    d = "".join(c for c in str(value or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def _orders_matching_phone(orders, phone):
    """Keep only orders whose stored phone actually matches the queried number. Shopify's
    customer/order search is FUZZY (token-based) and can return a DIFFERENT customer's order for
    a number it doesn't really have -> that showed a stranger's shipment (the reported bug). An
    order with NO phone at all is kept (we can't disprove it); a DIFFERENT phone is dropped."""
    want = _last10(phone)
    if not want:
        return orders
    kept = []
    for o in orders:
        cand = _last10(o.get("customer_phone"))
        if not cand or cand == want:
            kept.append(o)
        else:
            logger.info("SHOPIFY_PHONE_MISMATCH dropped order=%s order_phone=%s != queried=%s",
                        o.get("order_id") or o.get("name") or "-", cand, want)
    return kept


def _pick_customer_name(order):
    """The verified Shopify CUSTOMER name, robust to bad data: try customer / shipping / billing
    name fields in order and return the FIRST that looks like a real person name -- never an
    address fragment. Blank (-> 'Unknown') if none qualify, which beats showing an address."""
    cust = order.get("customer") or {}
    ship = order.get("shipping_address") or {}
    bill = order.get("billing_address") or {}

    def full(d):
        return f"{(d.get('first_name') or '').strip()} {(d.get('last_name') or '').strip()}".strip()

    candidates = [full(cust), full(ship), full(bill),
                  (cust.get("name") or "").strip(), (ship.get("name") or "").strip(),
                  (bill.get("name") or "").strip()]
    return next((c for c in candidates if _looks_like_person_name(c)), "")


class ShopifyClient:
    """Order + fulfillment + EDD lookup (doc section 8, Shopify)."""

    def __init__(self, shop, token, api_version="2024-10"):
        self.shop = shop
        self.token = token
        self.api_version = api_version

    @property
    def _headers(self):
        return {"X-Shopify-Access-Token": self.token}

    def _orders_url(self):
        return f"https://{self.shop}/admin/api/{self.api_version}/orders.json"

    def get_order(self, order_id):
        """Return a normalized order dict, or None."""
        requests = _requests()
        resp = requests.get(
            self._orders_url(), headers=self._headers,
            params={"name": order_id, "status": "any"}, timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        orders = resp.json().get("orders", [])
        if not orders:
            return None
        return self.normalize_order(orders[0])

    def recent_orders_by_email(self, email, limit=5):
        """Recent orders for a customer email (self-lookup: the sender email is a
        search key when no order number was given). Newest first, normalized."""
        requests = _requests()
        resp = requests.get(
            self._orders_url(), headers=self._headers,
            params={"email": email, "status": "any", "limit": limit},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return [self.normalize_order(o) for o in resp.json().get("orders", [])]

    @staticmethod
    def _phone_search_variants(phone):
        """Every format Shopify might have STORED this mobile under, so a customer who typed
        a bare 10-digit number still matches an order saved in E.164 (+91...). Shopify's
        customer search is token-based and does NOT normalize, so we must query each form."""
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) != 10:
            return [str(phone or "")] if phone else []
        return [digits, f"+91{digits}", f"91{digits}", f"0{digits}", f"+91 {digits}"]

    def _order_names_by_phone(self, phone, limit=5):
        """Recent order NAMES whose phone matches, via the GraphQL Admin ORDER search. Unlike
        customers/search this ALSO finds GUEST / COD orders that have NO customer record (the
        phone lives only on the order / shipping address) -- the reported "valid number won't
        verify" bug. Best-effort: returns [] on any error so it can only ADD matches."""
        requests = _requests()
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) != 10:
            return []
        q = " OR ".join(f"phone:{v}" for v in (digits, f"+91{digits}", f"91{digits}"))
        gql = ("query($q:String!,$n:Int!){orders(first:$n,query:$q,sortKey:CREATED_AT,"
               "reverse:true){edges{node{name}}}}")
        try:
            resp = requests.post(
                f"https://{self.shop}/admin/api/{self.api_version}/graphql.json",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"query": gql, "variables": {"q": q, "n": limit}},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            edges = ((((resp.json() or {}).get("data") or {}).get("orders") or {})
                     .get("edges") or [])
            names = [n for n in ((e.get("node") or {}).get("name") for e in edges) if n]
            logger.info("SHOPIFY_ORDERS_BY_PHONE phone=%s matches=%d", phone, len(names))
            return names
        except Exception as exc:  # noqa: BLE001 -- best-effort fallback, never raises
            logger.warning("SHOPIFY_ORDERS_BY_PHONE phone=%s failed: %s", phone, exc)
            return []

    def recent_orders_by_phone(self, phone, limit=5):
        """Recent orders for a phone: find the customer (trying every stored phone format),
        then their orders. Falls back to an ORDER search (guest/COD orders have no customer
        record). Logs SHOPIFY_LOOKUP_BY_PHONE / MATCH_FOUND / MATCH_FAILED."""
        requests = _requests()
        variants = self._phone_search_variants(phone)
        logger.info("SHOPIFY_LOOKUP_BY_PHONE phone=%s variants=%s", phone, variants)
        customers = []
        matched_variant = ""
        for variant in variants:
            cs = requests.get(
                f"https://{self.shop}/admin/api/{self.api_version}/customers/search.json",
                headers=self._headers, params={"query": f"phone:{variant}"},
                timeout=DEFAULT_TIMEOUT,
            )
            cs.raise_for_status()
            found = cs.json().get("customers", [])
            logger.info("SHOPIFY_PHONE_QUERY variant=%s customers=%d", variant, len(found))
            if found:
                customers, matched_variant = found, variant
                break
        if not customers:
            # No CUSTOMER record -> the phone may belong to a GUEST / COD order. Search orders
            # directly by phone, then fetch each matched order normalized.
            names = self._order_names_by_phone(phone, limit=limit)
            orders = []
            for name in names:
                try:
                    o = self.get_order(name)
                except Exception:  # noqa: BLE001 -- skip an order we can't fetch
                    o = None
                if o:
                    orders.append(o)
            orders = _orders_matching_phone(orders, phone)   # drop fuzzy-search mismatches
            if orders:
                logger.info("SHOPIFY_PHONE_MATCH_FOUND_VIA_ORDERS phone=%s orders=%d",
                            phone, len(orders))
                return orders
            logger.info("SHOPIFY_PHONE_MATCH_FAILED phone=%s (no customer or order match)",
                        phone)
            return []
        resp = requests.get(
            self._orders_url(), headers=self._headers,
            params={"customer_id": customers[0]["id"], "status": "any", "limit": limit},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        orders = _orders_matching_phone(
            [self.normalize_order(o) for o in resp.json().get("orders", [])], phone)
        logger.info("SHOPIFY_PHONE_MATCH_FOUND phone=%s matched_variant=%s customer_id=%s "
                    "orders=%d", phone, matched_variant, customers[0]["id"], len(orders))
        return orders

    @staticmethod
    def normalize_order(order):
        """Map a raw Shopify order to engine facts (also used to normalize fakes)."""
        fulfillment = (order.get("fulfillment_status") or "").lower()
        fulfillments = order.get("fulfillments") or []
        # ALL shipments: an order can be fulfilled in several packages, each with its own
        # tracking number (a single fulfillment can even carry several). Collect EVERY tracking
        # number across EVERY fulfillment so the tracking flow checks each one -- not just the
        # first. Deduped by AWB, in order. `shipments` = [{awb, courier, tracking_url}].
        shipments = []
        seen_awbs = set()
        for f in fulfillments:
            company = (f.get("tracking_company") or f.get("carrier") or "")
            numbers = f.get("tracking_numbers") or (
                [f.get("tracking_number")] if f.get("tracking_number") else [])
            urls = f.get("tracking_urls") or (
                [f.get("tracking_url")] if f.get("tracking_url") else [])
            for i, num in enumerate(numbers):
                num = (num or "").strip()
                if not num or num in seen_awbs:
                    continue
                seen_awbs.add(num)
                url = urls[i] if i < len(urls) else (urls[0] if urls else "")
                shipments.append({"awb": num, "courier": company, "tracking_url": url or ""})
        # Back-compat single fields = the FIRST shipment (existing single-tracking behavior).
        tracking_url = shipments[0]["tracking_url"] if shipments else ""
        awb = shipments[0]["awb"] if shipments else ""
        courier = shipments[0]["courier"] if shipments else ""
        if not shipments and fulfillments:
            # A fulfillment with a tracking URL but no number -> keep the URL for the link.
            tracking_url = (fulfillments[0].get("tracking_url")
                            or (fulfillments[0].get("tracking_urls") or [""])[0] or "")
            courier = (fulfillments[0].get("tracking_company")
                       or fulfillments[0].get("carrier") or "")
        line_items = order.get("line_items") or []
        custom_item = any(li.get("custom") or not li.get("product_id") for li in line_items)
        name = order.get("name") or ""
        # Verified Shopify CUSTOMER name -- robust to bad data (an address typed into a name
        # field): picks the first customer/shipping/billing value that looks like a real name,
        # never an address fragment like "1/166 - 42".
        cust = order.get("customer") or {}
        first = (cust.get("first_name") or "").strip()
        last = (cust.get("last_name") or "").strip()
        customer_name = _pick_customer_name(order)
        # Verified Shopify CUSTOMER phone (customer -> shipping/billing address -> order).
        ship = order.get("shipping_address") or {}
        bill = order.get("billing_address") or {}
        customer_phone = (cust.get("phone") or ship.get("phone") or bill.get("phone")
                          or order.get("phone") or "").strip()
        # Verified Shopify CUSTOMER email (customer.email -> order contact_email -> order email).
        customer_email = (cust.get("email") or order.get("contact_email")
                          or order.get("email") or "").strip()
        # Full payload + the status-bearing fields, so a "wrong status" report can be traced
        # to the exact raw Shopify values (req: log the COMPLETE order payload).
        try:
            import json as _json
            logger.info("SHOPIFY-ORDER-PAYLOAD order=%s %s", name or order.get("id"),
                        _json.dumps(order, default=str)[:4000])
        except Exception:  # noqa: BLE001 -- logging must never break the lookup
            pass
        logger.info("SHOPIFY-RAW-STATUS fulfillment_status=%s financial_status=%s "
                    "cancelled_at=%s cancel_reason=%s order_status_url=%s",
                    order.get("fulfillment_status"), order.get("financial_status"),
                    order.get("cancelled_at"), order.get("cancel_reason"),
                    order.get("order_status_url"))
        return {
            "name": name,
            "order_id": name or str(order.get("id") or ""),
            # RAW STATUS sources, kept EXACTLY as Shopify returned them (no mapping). The
            # tracking email prefers cancellation, then courier, then fulfillment, then order.
            "cancelled_at": order.get("cancelled_at"),
            "cancel_reason": order.get("cancel_reason") or "",
            "order_status_url": order.get("order_status_url") or "",
            "raw_fulfillment_status": order.get("fulfillment_status") or "",
            "raw_order_status": (order.get("financial_status")
                                 or order.get("status") or ""),
            "shipped": fulfillment in ("fulfilled", "partial"),
            "dispatched": bool(fulfillments) or fulfillment in ("fulfilled", "partial"),
            "delivered": (order.get("fulfillment_status") == "fulfilled"
                          and bool(order.get("delivered_at"))),
            "edd": order.get("estimated_delivery_at") or order.get("edd") or "",
            "tracking_url": tracking_url,
            "awb": awb,
            "courier": courier,
            "shipments": shipments,          # every tracking number across all fulfillments
            "custom_item": custom_item,
            "financial_status": (order.get("financial_status") or "").lower(),
            "customer_name": customer_name,
            "customer_first_name": first,
            "customer_last_name": last,
            "customer_phone": customer_phone,
            "customer_email": customer_email,
        }


class ShippingClient:
    """Tracking + EDD by AWB (doc section 8, Shipping Portal)."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def track(self, awb):
        requests = _requests()
        resp = requests.get(
            f"{self.base_url}/track/{awb}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return self.normalize_tracking(resp.json())

    def status_by_order(self, order_id):
        """Shipping/Courier-panel status for an ORDER (no AWB needed) -- this is the panel
        that can read 'Cancelled' for an order Shopify still reports as 'fulfilled'.
        Best-effort: returns a normalized dict or None. The endpoint path is configurable
        via SHIPPING_ORDER_STATUS_PATH (default '/order/{order_id}')."""
        from django.conf import settings as dj

        path = getattr(dj, "SHIPPING_ORDER_STATUS_PATH", "/order/{order_id}")
        requests = _requests()
        resp = requests.get(
            f"{self.base_url}{path.format(order_id=order_id)}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        return self.normalize_tracking(data) if data else None

    @staticmethod
    def normalize_tracking(data):
        raw = (data.get("status") or data.get("current_status") or "").strip()
        # `status` is the lower/underscored form used ONLY for internal flags
        # (shipped/delivered). `raw_status` is the EXACT courier string shown to the
        # customer -- never mapped, grouped, or simplified (RAW STATUS mode).
        status = raw.lower().replace(" ", "_")
        return {
            "status": status,
            "raw_status": raw,
            "delivered": status == "delivered",
            "shipped": status in ("in_transit", "out_for_delivery", "shipped", "delivered"),
            "edd": data.get("edd") or data.get("expected_delivery") or "",
            "tracking_url": data.get("tracking_url") or "",
            "courier": data.get("courier") or data.get("carrier") or "",
            "awb": data.get("awb") or data.get("tracking_number") or "",
        }


class GoKwikClient:
    """Payment status lookup (doc section 8, GoKwik)."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get_payment(self, order_id):
        requests = _requests()
        resp = requests.get(
            f"{self.base_url}/payments",
            headers={"Authorization": f"Bearer {self.api_key}"},
            params={"order_id": order_id},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return self.normalize_payment(resp.json())

    @staticmethod
    def normalize_payment(data):
        payments = data.get("payments") if isinstance(data, dict) else data
        payments = payments or []
        captured = [p for p in payments if (p.get("status") or "").lower() == "captured"]
        return {
            "paid": bool(captured),
            "double_payment": len(captured) > 1,
            "amount": sum(float(p.get("amount", 0) or 0) for p in captured),
        }


def build_clients(settings):
    """Build the configured clients from BrandSettings.integrations, falling back to the
    global .env values (settings.SHOPIFY_* / SHIPPING_*) for any integration the brand
    hasn't configured in the DB. Per-brand DB config always WINS.

    Returns a dict {"shopify": .., "shipping": .., "gokwik": ..} with None for
    any integration that isn't configured. Never raises.
    """
    from django.conf import settings as dj

    cfg = (settings.integrations if settings else None) or {}
    clients = {"shopify": None, "shipping": None, "gokwik": None}

    sh = cfg.get("shopify") or {}
    shop = sh.get("shop") or getattr(dj, "SHOPIFY_SHOP", "")
    token = sh.get("token") or getattr(dj, "SHOPIFY_TOKEN", "")
    if shop and token:
        clients["shopify"] = ShopifyClient(
            shop, token, sh.get("api_version") or getattr(dj, "SHOPIFY_API_VERSION", "2024-10")
        )

    sp = cfg.get("shipping") or {}
    base_url = sp.get("base_url") or getattr(dj, "SHIPPING_BASE_URL", "")
    api_key = sp.get("api_key") or getattr(dj, "SHIPPING_API_KEY", "")
    if base_url and api_key:
        clients["shipping"] = ShippingClient(base_url, api_key)
        logger.info("SHIPPING-PANEL-ENABLED base_url=%s -> shipping/courier panel WILL be "
                    "queried.", base_url)
    else:
        logger.info("SHIPPING-PANEL-DISABLED base_url=%s api_key_set=%s -> shipping/courier "
                    "panel is NOT queried; status falls back to Shopify (this is why a panel "
                    "'Cancelled' is not seen).", base_url or "(unset)", bool(api_key))

    gk = cfg.get("gokwik") or {}
    if gk.get("base_url") and gk.get("api_key"):
        clients["gokwik"] = GoKwikClient(gk["base_url"], gk["api_key"])

    return clients
