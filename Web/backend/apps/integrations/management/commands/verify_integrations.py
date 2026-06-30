"""
Verify every order-tracking integration for a brand and print the EXACT result --
no guessing: real Shopify Admin API + courier-tracking calls, with status codes and
raw response bodies.

    python manage.py verify_integrations                 # active brand, order 262134021
    python manage.py verify_integrations --brand 1 --order 262134021
"""

import json

from django.core.management.base import BaseCommand

DEFAULT_ORDER = "262134021"


def _resolve_brand(ref):
    """Resolve a brand by id / slug / name, or the first active brand."""
    from apps.organizations.models import Brand

    if ref:
        qs = Brand.objects.all()
        if str(ref).isdigit():
            b = qs.filter(id=int(ref)).first()
            if b:
                return b
        return qs.filter(slug=ref).first() or qs.filter(name__iexact=ref).first()
    return Brand.objects.filter(is_active=True).order_by("id").first() \
        or Brand.objects.order_by("id").first()


def _brand_integrations(brand):
    from apps.brand_settings.models import BrandSettings

    try:
        return (brand.settings.integrations or {}), brand.settings
    except BrandSettings.DoesNotExist:
        return {}, None


def _short(text, n=600):
    text = text or ""
    return text if len(text) <= n else text[:n] + f"... ({len(text)} bytes total)"


def run_verification(brand, order_id, out):
    """Run all checks, printing to `out` (a Command). Returns True if fully operational."""
    import sys

    import requests

    from apps.integrations.clients import ShopifyClient

    # The Windows console defaults to cp1252, which can't encode the ✓ / ✗ marks.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not all streams support reconfigure
            pass

    w = out.stdout.write
    ok = out.style.SUCCESS
    bad = out.style.ERROR
    cfg, _settings = _brand_integrations(brand)
    sh = (cfg.get("shopify") or {})
    sp = (cfg.get("shipping") or {})

    shop, token = sh.get("shop") or "", sh.get("token") or ""
    api_version = sh.get("api_version") or "2024-10"
    shopify_ok = False
    order = None
    failure_reason = ""

    # --- 1) SHOPIFY ----------------------------------------------------------------
    w("\nSHOPIFY")
    w(f"  shopify.shop  : {shop or '(empty)'}")
    w(f"  shopify.token : {('SET (' + str(len(token)) + ' chars)') if token else '(empty)'}")
    if not shop or not token:
        w(bad("✗ Not Configured (missing shop and/or token)"))
        failure_reason = "Shopify not configured (shopify.shop / shopify.token missing)."
    else:
        url = f"https://{shop}/admin/api/{api_version}/orders.json"
        try:
            resp = requests.get(url, headers={"X-Shopify-Access-Token": token},
                                params={"name": order_id, "status": "any"}, timeout=20)
            w(f"  GET {resp.url}")
            w(f"  HTTP {resp.status_code}")
            if resp.status_code == 200:
                shopify_ok = True
                w(ok("✓ Connected"))
            else:
                w(bad(f"✗ {resp.status_code} {resp.reason}"))
                w(f"  body: {_short(resp.text)}")
                failure_reason = f"Shopify API returned HTTP {resp.status_code} {resp.reason}."
        except Exception as exc:  # noqa: BLE001
            w(bad(f"✗ Request failed: {type(exc).__name__}: {exc}"))
            failure_reason = f"Shopify request error: {type(exc).__name__}: {exc}"
            resp = None

        # --- 2) ORDER LOOKUP -------------------------------------------------------
        w("\nORDER LOOKUP")
        w(f"  order: {order_id}")
        if shopify_ok and resp is not None:
            try:
                orders = resp.json().get("orders", [])
            except ValueError:
                orders = []
                w(bad("  (response was not valid JSON)"))
            if not orders:
                w(bad("✗ Order Not Found (Shopify returned 0 orders for this number)"))
                if not failure_reason:
                    failure_reason = f"Order {order_id} not found in Shopify."
            else:
                raw = orders[0]
                order = ShopifyClient.normalize_order(raw)
                w(ok("✓ Order Found"))
                w(f"  Order ID    : {order.get('order_id')}")
                w(f"  Fulfillment : {raw.get('fulfillment_status') or '(none)'} "
                  f"(shipped={order.get('shipped')}, delivered={order.get('delivered')})")
                w(f"  Tracking URL: {order.get('tracking_url') or '(none)'}")
                w(f"  AWB         : {order.get('awb') or '(none)'}")
                w(f"  Courier     : {order.get('courier') or '(none)'}")
                w(f"  EDD         : {order.get('edd') or '(none)'}")
                w("  --- raw Shopify order (truncated) ---")
                w("  " + _short(json.dumps(raw), 800))
        else:
            w("  (skipped -- Shopify not connected)")

    # --- 3) SHIPPING / COURIER -----------------------------------------------------
    w("\nSHIPPING")
    base_url, api_key = sp.get("base_url") or "", sp.get("api_key") or ""
    w(f"  shipping.base_url: {base_url or '(empty)'}")
    w(f"  shipping.api_key : {('SET (' + str(len(api_key)) + ' chars)') if api_key else '(empty)'}")
    shipping_ok = False
    if not base_url or not api_key:
        w(bad("✗ Not Configured"))
    else:
        test_awb = (order or {}).get("awb") or "TEST-AWB"
        track_url = f"{base_url.rstrip('/')}/track/{test_awb}"
        try:
            resp = requests.get(track_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
            w(f"  GET {track_url}")
            w(f"  HTTP {resp.status_code}")
            w(f"  body: {_short(resp.text)}")
            if resp.status_code == 200:
                shipping_ok = True
                w(ok("✓ Connected"))
            else:
                w(bad(f"✗ {resp.status_code} {resp.reason}"))
        except Exception as exc:  # noqa: BLE001
            w(bad(f"✗ Request failed: {type(exc).__name__}: {exc}"))

    # --- FINAL RESULT --------------------------------------------------------------
    has_tracking = bool(order and (order.get("awb") or order.get("tracking_url")
                                   or order.get("shipped")))
    fully_operational = shopify_ok and order is not None and has_tracking
    w("\nFINAL RESULT")
    if fully_operational:
        w(ok("✓ Order tracking fully operational"))
    else:
        if not failure_reason:
            if order is None:
                failure_reason = "Order lookup did not return an order."
            elif not has_tracking:
                failure_reason = ("Order found but has no fulfillment/AWB/tracking yet "
                                  "(not shipped) -- and no courier integration to query.")
            else:
                failure_reason = "Unknown."
        if not shipping_ok and shopify_ok and order is not None and not has_tracking:
            failure_reason += " Shipping/courier integration is not connected."
        w(bad("✗ Tracking unavailable because:"))
        w(bad(f"  {failure_reason}"))
    return fully_operational


class Command(BaseCommand):
    help = "Verify Shopify + courier order-tracking integrations for a brand (real API calls)."

    def add_arguments(self, parser):
        parser.add_argument("--brand", default="", help="Brand id / slug / name (default: active).")
        parser.add_argument("--order", default=DEFAULT_ORDER, help="Order number to test.")

    def handle(self, *args, **o):
        brand = _resolve_brand(o["brand"])
        if not brand:
            self.stderr.write("No brand found.")
            return
        self.stdout.write(f"Brand: {brand} (id={brand.id})")
        run_verification(brand, o["order"], self)
