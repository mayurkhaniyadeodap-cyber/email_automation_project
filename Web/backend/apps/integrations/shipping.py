"""
ship.deodap.com AWB verification (DeoDap Care — Final Mail Flow v2.0, §5 & §9).

The AWB / tracking number is extracted on the AI path only (no blind regex auto-
action, §9). Before the Mail Engine *uses* an AWB -- to answer a tracking query or
to attach it to a Care Panel ticket -- it verifies the number against the shipping
portal. An AWB the portal doesn't recognize is left unverified (we don't act on it).

Config-gated: with no shipping client configured (BrandSettings.integrations
["shipping"]), verification is skipped gracefully -- `verify_awb` returns None and
`annotate_awb_verification` is a no-op, so nothing breaks when creds are absent.
"""

import logging

logger = logging.getLogger(__name__)


def _clients(brand, clients):
    if clients is not None:
        return clients
    from apps.integrations import context

    return context.build_clients(context._settings_for(brand))


def verify_awb(brand, awb, clients=None):
    """Return normalized tracking (status/edd/tracking_url) if ship.deodap.com knows
    this AWB, else None. None also means 'cannot verify' (portal not configured)."""
    awb = (awb or "").strip()
    if not awb:
        return None
    shipping = _clients(brand, clients).get("shipping")
    if shipping is None:
        return None  # portal not configured -> cannot verify
    try:
        return shipping.track(awb)
    except Exception:  # noqa: BLE001 -- verification is best-effort
        logger.exception("AWB verify failed for %s", awb)
        return None


def annotate_awb_verification(ticket, clients=None):
    """Verify the ticket's AI-extracted AWB and record the result on `extracted`.

    Sets `awb_verified` (bool) and, when verified, fills `tracking_url` / `edd`
    from the live portal data. Returns the tracking dict (or None). Best-effort and
    idempotent; a no-op when there is no AWB or no shipping integration.
    """
    extracted = ticket.extracted or {}
    awb = extracted.get("awb")
    if not awb:
        return None

    tracking = verify_awb(ticket.brand, awb, clients=clients)
    extracted = {**extracted, "awb_verified": bool(tracking)}
    if tracking:
        if tracking.get("tracking_url") and not ticket.tracking_url:
            extracted["tracking_url"] = tracking["tracking_url"]
        if tracking.get("edd"):
            extracted["edd"] = tracking["edd"]
    ticket.extracted = extracted
    ticket.save(update_fields=["extracted", "updated_at"])

    from apps.tickets.models import AuditLogEntry

    AuditLogEntry.objects.create(
        ticket=ticket, actor="system", event="awb_verified",
        detail={"awb": awb, "verified": bool(tracking),
                "status": (tracking or {}).get("status", "")})
    logger.info("AWB-VERIFY ticket=%s awb=%s verified=%s", ticket.ticket_id, awb, bool(tracking))
    return tracking
