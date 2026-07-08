"""Care Panel -> Mail Engine TICKET STATUS SYNCHRONIZATION.

care.deodap.in (the Care Panel) is the source of truth for agent-set ticket status. This module is
shared by BOTH inbound paths so there is exactly one mapping and one apply routine:

  1. The real-time WEBHOOK  (apps.integrations.webhooks.care_panel_webhook) -- if the panel POSTs a
     status change, it is applied immediately.
  2. The POLLING job        (management command `sync_care_panel_status`, run by cron) -- reconciles
     status by reading the Care Panel open-tickets API, so a change is picked up even when the panel
     does NOT fire a webhook (the reported bug: an agent CLOSED a ticket in the panel but it still
     showed In Progress here, because only ticket CREATION was ever synced and no job pulled updates).

Every synchronization event is logged (CARE_PANEL_STATUS_SYNC / _DONE) and audited; updates are
idempotent so re-runs never duplicate.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.tickets.models import AuditLogEntry, Ticket

logger = logging.getLogger(__name__)

# Care Panel status string -> internal Ticket status. Case-insensitive; spaces/hyphens -> '_'.
# The 7 supported agent statuses map onto the Mail Engine lifecycle. The terminal states
# (Resolved / Closed) map EXACTLY -- which is what the reported bug requires.
#   Open / In Progress / Awaiting Customer / Pending / Reopened  -> in_progress  (active)
#   Resolved                                                     -> resolved
#   Closed                                                       -> closed
STATUS_MAP = {
    "open": Ticket.STATUS_IN_PROGRESS,
    "in_progress": Ticket.STATUS_IN_PROGRESS,
    "in_process": Ticket.STATUS_IN_PROGRESS,
    "inprocess": Ticket.STATUS_IN_PROGRESS,
    "processing": Ticket.STATUS_IN_PROGRESS,
    "pending": Ticket.STATUS_IN_PROGRESS,
    "awaiting_customer": Ticket.STATUS_IN_PROGRESS,
    "awaiting_customer_reply": Ticket.STATUS_IN_PROGRESS,
    "waiting_for_customer": Ticket.STATUS_IN_PROGRESS,
    "reopened": Ticket.STATUS_IN_PROGRESS,
    "reopen": Ticket.STATUS_IN_PROGRESS,
    "resolved": Ticket.STATUS_RESOLVED,
    "completed": Ticket.STATUS_RESOLVED,
    "closed": Ticket.STATUS_CLOSED,
    "escalated": Ticket.STATUS_ESCALATED,
    # --- Additional Care Panel agent statuses (additive; existing mappings above unchanged) ---
    # Active holds -> keep the ticket In Progress (the Mail Engine has no distinct hold status).
    "hold_waiting_for_customer": Ticket.STATUS_IN_PROGRESS,
    "hold_waiting_for_others": Ticket.STATUS_IN_PROGRESS,
    "waiting_for_courier_update": Ticket.STATUS_IN_PROGRESS,
    # Terminal closure reasons -> Closed.
    "closed_no_response": Ticket.STATUS_CLOSED,
    "closed_no_solution": Ticket.STATUS_CLOSED,
    "closed_with_solution": Ticket.STATUS_CLOSED,
    "duplicate": Ticket.STATUS_CLOSED,
}


def normalize(value):
    """Map an external Care Panel status string to an internal Ticket status, or None. Separators
    are normalized to '_' and collapsed, so 'Closed - No Response' and 'Closed No Response' both
    resolve to the same key (existing single-underscore keys are unaffected)."""
    import re

    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = re.sub(r"_+", "_", key).strip("_")
    return STATUS_MAP.get(key)


def apply_status(ticket, new_status, *, source, raw=""):
    """Idempotently mirror an external status onto `ticket`. Returns True iff it CHANGED.

    Logs a CARE_PANEL_STATUS_SYNC line for EVERY call (change or no-change) and audits a
    'status_mirrored' entry on change. Sets resolved_at when moving into a terminal state."""
    tid = getattr(ticket, "ticket_id", "?")
    if not new_status or new_status == ticket.status:
        logger.info("CARE_PANEL_STATUS_SYNC ticket=%s source=%s raw=%r mapped=%s current=%s "
                    "-> NO_CHANGE", tid, source, raw, new_status or "-", ticket.status)
        return False
    old = ticket.status
    ticket.status = new_status
    fields = ["status", "updated_at"]
    if new_status in (Ticket.STATUS_RESOLVED, Ticket.STATUS_CLOSED) and not ticket.resolved_at:
        ticket.resolved_at = timezone.now()
        fields.append("resolved_at")
    ticket.save(update_fields=fields)
    AuditLogEntry.objects.create(
        ticket=ticket, actor=source, event="status_mirrored",
        detail={"from": old, "to": new_status, "raw": str(raw), "source": source})
    logger.info("CARE_PANEL_STATUS_SYNC ticket=%s source=%s raw=%r -> UPDATED %s -> %s",
                tid, source, raw, old, new_status)
    return True


def sync_statuses_from_care_panel(*, grace_minutes=10, client_for=None, now=None):
    """POLLING reconcile: for every ACTIVE local ticket that was pushed to the Care Panel (has a
    care_panel_ticket_id + phone), read the panel's open-tickets list and mirror the status.

    - Present in the open list  -> mirror its `status` field (In-process/Pending/... -> internal).
    - Absent from the open list -> the ticket is no longer open in the panel (agent Closed/Resolved
      it) -> mark it CLOSED. This is the fix for statuses that disappear from the open list.

    Idempotent (terminal tickets are excluded next run), grace-guarded (skips very new tickets to
    avoid racing creation), and best-effort (a failed/!ok lookup never closes tickets). Reuses the
    existing CarePanelClient open-tickets API. Returns (checked, updated, closed).
    `client_for(brand)` is injectable for tests; defaults to care_panel.build_client_for."""
    from collections import defaultdict

    from apps.integrations import care_panel

    now = now or timezone.now()
    cutoff = now - timedelta(minutes=grace_minutes)
    client_for = client_for or care_panel.build_client_for

    qs = (Ticket.objects.exclude(status__in=Ticket.TERMINAL_STATUSES)
          .filter(is_ignored=False, created_at__lte=cutoff))
    groups = defaultdict(list)                       # (brand_id, phone) -> [tickets]
    skipped = 0
    for t in qs:
        ex = t.extracted or {}
        cpid = str(ex.get("care_panel_ticket_id") or "").strip()
        phone = str(ex.get("phone") or "").strip()
        if cpid and phone:
            groups[(t.brand_id, phone)].append(t)
        else:
            skipped += 1

    checked = updated = closed = 0
    for (brand_id, phone), tickets in groups.items():
        brand = tickets[0].brand
        client = client_for(brand)
        if client is None:
            logger.info("CARE_PANEL_STATUS_SYNC brand=%s phone=%s -> SKIP (no client configured)",
                        brand_id, phone)
            continue
        try:
            resp = client.lookup(phone=phone, email=tickets[0].customer_email,
                                 order_id=(tickets[0].extracted or {}).get("order_id"))
        except Exception as exc:  # noqa: BLE001 -- best-effort; a failed lookup never closes tickets
            logger.warning("CARE_PANEL_STATUS_SYNC phone=%s -> lookup ERROR %s (skip group)",
                           phone, exc)
            continue
        if not isinstance(resp, dict):
            continue
        api_ok = resp.get("success", True) is not False
        open_map = {}
        for pt in (resp.get("tickets") or []):
            pid = str(pt.get("id") or pt.get("ticket_id") or pt.get("_id") or "").strip()
            if pid:
                open_map[pid] = pt.get("status") or ""
        for t in tickets:
            checked += 1
            cpid = str((t.extracted or {}).get("care_panel_ticket_id") or "").strip()
            if cpid in open_map:
                if apply_status(t, normalize(open_map[cpid]), source="care_panel_poll",
                                raw=open_map[cpid]):
                    updated += 1
            elif api_ok:
                # Gone from the open-tickets list -> Closed/Resolved in the panel -> mark CLOSED.
                if apply_status(t, Ticket.STATUS_CLOSED, source="care_panel_poll",
                                raw="absent_from_open_tickets"):
                    closed += 1
            else:
                logger.info("CARE_PANEL_STATUS_SYNC ticket=%s -> SKIP closure (api success=false)",
                            t.ticket_id)

    logger.info("CARE_PANEL_STATUS_SYNC_DONE checked=%s updated=%s closed=%s skipped_no_id_or_phone=%s",
                checked, updated, closed, skipped)
    return checked, updated, closed
