"""
Gallabox sync (the customer's spec). Before/after a ticket is created or updated,
mirror it to Gallabox: search by customer email / order id / phone, then update the
existing Gallabox ticket (append the conversation) or create a new one.

The HTTP client is thin, lazy (`requests`), and injectable, so the sync ORCHESTRATION
(search -> update-or-create) is fully unit-testable with a fake. Per-brand credentials
live on BrandSettings.integrations["gallabox"]:

    {"gallabox": {"api_key": "...", "api_secret": "...",
                  "base_url": "https://server.gallabox.com/devapi"}}

Endpoint paths are configurable in case your Gallabox account exposes them differently.
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://server.gallabox.com/devapi"
DEFAULT_TIMEOUT = 10


def _requests():
    import requests

    return requests


class GallaboxClient:
    def __init__(self, api_key, api_secret, base_url=DEFAULT_BASE):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")

    @property
    def _headers(self):
        return {"apiKey": self.api_key, "apiSecret": self.api_secret,
                "Content-Type": "application/json"}

    # -- low-level ---------------------------------------------------------
    def _get(self, path, params=None):
        r = _requests().get(f"{self.base_url}{path}", headers=self._headers,
                            params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload):
        r = _requests().post(f"{self.base_url}{path}", headers=self._headers,
                             json=payload, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _put(self, path, payload):
        r = _requests().put(f"{self.base_url}{path}", headers=self._headers,
                            json=payload, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()

    # -- ticket operations -------------------------------------------------
    def search_ticket(self, *, email=None, order_id=None, phone=None):
        """Return an existing OPEN Gallabox ticket dict, or None."""
        params = {}
        if email:
            params["email"] = email
        if order_id:
            params["orderId"] = order_id
        if phone:
            params["phone"] = phone
        if not params:
            return None
        data = self._get("/tickets/search", params=params)
        tickets = data.get("tickets") if isinstance(data, dict) else data
        for t in tickets or []:
            if str(t.get("status", "")).lower() not in ("closed", "resolved"):
                return t
        return None

    def create_ticket(self, payload):
        return self._post("/tickets", payload)

    def update_ticket(self, ticket_id, payload):
        return self._put(f"/tickets/{ticket_id}", payload)

    def add_message(self, ticket_id, text):
        return self._post(f"/tickets/{ticket_id}/messages", {"text": text})


def build_client(settings):
    """Build a Gallabox client from BrandSettings.integrations, or None."""
    cfg = ((settings.integrations if settings else None) or {}).get("gallabox") or {}
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        return None
    return GallaboxClient(cfg["api_key"], cfg["api_secret"], cfg.get("base_url"))


def _ticket_payload(ticket):
    extracted = ticket.extracted or {}
    return {
        "subject": ticket.subject,
        "customerEmail": ticket.customer_email,
        "orderId": extracted.get("order_id") or "",
        "category": ticket.category,
        "subCategory": ticket.sub_topic,
        "status": ticket.status,
        "priority": ticket.priority,
        "summary": extracted.get("issue_summary") or "",
        "externalId": ticket.ticket_id,
    }


def _settings_for(brand):
    from apps.brand_settings.models import BrandSettings

    try:
        return brand.settings
    except BrandSettings.DoesNotExist:
        return None


def build_client_for(brand):
    """Indirection so tests can monkeypatch client construction."""
    return build_client(_settings_for(brand))


def sync_ticket(ticket, client=None):
    """Mirror a ticket to Gallabox (spec: search -> update-or-create). Best-effort;
    returns the Gallabox ticket id, or None when not configured / on error."""
    if client is None:
        client = build_client_for(ticket.brand)
    if client is None:
        return None

    extracted = ticket.extracted or {}
    try:
        existing = client.search_ticket(
            email=ticket.customer_email,
            order_id=extracted.get("order_id"),
            phone=extracted.get("phone"),
        )
        payload = _ticket_payload(ticket)
        if existing:
            gid = existing.get("id") or existing.get("_id")
            client.update_ticket(gid, payload)
            if extracted.get("issue_summary"):
                client.add_message(gid, extracted["issue_summary"])
            from apps.tickets.models import AuditLogEntry

            AuditLogEntry.objects.create(
                ticket=ticket, actor="system", event="gallabox_ticket_matched",
                detail={"gallabox_id": str(gid)},
            )
        else:
            created = client.create_ticket(payload)
            gid = created.get("id") or created.get("_id")
        if gid:
            ticket.extracted = {**(ticket.extracted or {}), "gallabox_id": str(gid)}
            ticket.save(update_fields=["extracted", "updated_at"])
        return gid
    except Exception:  # noqa: BLE001 -- sync is best-effort
        logger.exception("Gallabox sync failed for %s", ticket.ticket_id)
        return None
