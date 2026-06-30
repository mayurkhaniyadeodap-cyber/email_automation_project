"""
Set a brand's order-tracking integration credentials (Shopify + courier) in
BrandSettings.integrations, then optionally verify them live.

    python manage.py set_integration --brand 1 \
        --shopify-shop deodap.myshopify.com --shopify-token shpat_xxx \
        --shipping-url https://ship.example/api --shipping-key sk_xxx --verify

Only the flags you pass are changed; the rest of the existing config is preserved.
"""

from django.core.management.base import BaseCommand

from .verify_integrations import DEFAULT_ORDER, _resolve_brand, run_verification


class Command(BaseCommand):
    help = "Set a brand's Shopify / shipping integration credentials (and optionally verify)."

    def add_arguments(self, parser):
        parser.add_argument("--brand", default="", help="Brand id / slug / name (default: active).")
        parser.add_argument("--shopify-shop", default=None, help="e.g. deodap.myshopify.com")
        parser.add_argument("--shopify-token", default=None, help="Admin API token (shpat_...).")
        parser.add_argument("--shopify-api-version", default=None, help="e.g. 2024-10")
        parser.add_argument("--shipping-url", default=None, help="Courier tracking API base URL.")
        parser.add_argument("--shipping-key", default=None, help="Courier tracking API key.")
        parser.add_argument("--order", default=DEFAULT_ORDER, help="Order to use with --verify.")
        parser.add_argument("--verify", action="store_true", help="Run live checks after saving.")

    def handle(self, *args, **o):
        from apps.brand_settings.models import BrandSettings

        brand = _resolve_brand(o["brand"])
        if not brand:
            self.stderr.write("No brand found (pass --brand <id|slug|name>).")
            return

        settings, _ = BrandSettings.objects.get_or_create(brand=brand)
        cfg = dict(settings.integrations or {})
        shopify = dict(cfg.get("shopify") or {})
        shipping = dict(cfg.get("shipping") or {})

        changed = []
        if o["shopify_shop"] is not None:
            # Accept a pasted URL too: strip scheme + trailing slash -> bare myshopify domain.
            shop_val = (o["shopify_shop"].strip()
                        .replace("https://", "").replace("http://", "").strip("/"))
            shopify["shop"] = shop_val; changed.append("shopify.shop")
        if o["shopify_token"] is not None:
            shopify["token"] = o["shopify_token"]; changed.append("shopify.token")
        if o["shopify_api_version"] is not None:
            shopify["api_version"] = o["shopify_api_version"]; changed.append("shopify.api_version")
        if o["shipping_url"] is not None:
            shipping["base_url"] = o["shipping_url"]; changed.append("shipping.base_url")
        if o["shipping_key"] is not None:
            shipping["api_key"] = o["shipping_key"]; changed.append("shipping.api_key")

        if shopify:
            cfg["shopify"] = shopify
        if shipping:
            cfg["shipping"] = shipping
        settings.integrations = cfg
        settings.save(update_fields=["integrations", "updated_at"])

        self.stdout.write(f"Brand: {brand} (id={brand.id})")
        if changed:
            self.stdout.write(self.style.SUCCESS(f"Saved: {', '.join(changed)}"))
        else:
            self.stdout.write("No fields passed -- nothing changed (showing current config).")
        # Echo the config with secrets masked.
        masked = {
            "shopify": {"shop": shopify.get("shop") or "(empty)",
                        "token": ("SET" if shopify.get("token") else "(empty)"),
                        "api_version": shopify.get("api_version") or "2024-10"},
            "shipping": {"base_url": shipping.get("base_url") or "(empty)",
                         "api_key": ("SET" if shipping.get("api_key") else "(empty)")},
        }
        self.stdout.write(f"Current integrations: {masked}")

        if o["verify"]:
            self.stdout.write("\n--- VERIFYING ---")
            run_verification(brand, o["order"], self)
