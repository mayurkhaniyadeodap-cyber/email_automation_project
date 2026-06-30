"""
Analytics reports (doc section 13, Phase 6): SLA dashboards, AI-accuracy reports,
agent performance, and ticket volume. Pure functions over a *pre-scoped* ticket
queryset (the caller applies org/brand/date filters), so they're trivially testable.
"""

from collections import Counter
import datetime

from django.db.models import Avg, Count  # type: ignore[import]
try:
    from django.utils import timezone  # type: ignore[import]
except ImportError:
    class timezone:
        @staticmethod
        def now():
            return datetime.datetime.now(datetime.timezone.utc)

        timedelta = datetime.timedelta

from apps.tickets.models import AuditLogEntry, Ticket

DEFAULT_DUE_SOON_MINUTES = 120
DEFAULT_LOW_CONFIDENCE = 0.75


def _audit_qs(tickets):
    return AuditLogEntry.objects.filter(ticket__in=tickets)


def volume_report(tickets):
    """Ticket counts by status / priority / category and the headline tallies."""
    total = tickets.count()
    by_status = dict(
        tickets.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    by_priority = dict(
        tickets.values_list("priority").annotate(n=Count("id")).values_list("priority", "n")
    )
    by_category = dict(
        tickets.exclude(category="")
        .values_list("category")
        .annotate(n=Count("id"))
        .values_list("category", "n")
    )
    open_count = (
        tickets.filter(is_ignored=False)
        .exclude(status__in=Ticket.TERMINAL_STATUSES)
        .count()
    )
    return {
        "total": total,
        "open": open_count,
        "ignored": tickets.filter(is_ignored=True).count(),
        "auto_resolved": tickets.filter(status=Ticket.STATUS_AUTO_RESOLVED).count(),
        "by_status": by_status,
        "by_priority": by_priority,
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
    }


def sla_report(tickets, now=None, due_soon_minutes=DEFAULT_DUE_SOON_MINUTES):
    """SLA compliance: breached / due-soon for open tickets, met / missed for closed."""
    now = now or timezone.now()
    soon = now + timezone.timedelta(minutes=due_soon_minutes)

    active = tickets.filter(is_ignored=False).exclude(status__in=Ticket.TERMINAL_STATUSES)
    with_sla = active.exclude(sla_due_at=None)
    breached = with_sla.filter(sla_due_at__lt=now).count()
    due_soon = with_sla.filter(sla_due_at__gte=now, sla_due_at__lte=soon).count()

    closed = tickets.filter(status__in=Ticket.TERMINAL_STATUSES).exclude(sla_due_at=None)
    met = missed = 0
    for due, resolved in closed.values_list("sla_due_at", "resolved_at"):
        if resolved is None:
            continue
        if resolved <= due:
            met += 1
        else:
            missed += 1
    judged = met + missed
    compliance = round(met / judged, 4) if judged else None

    return {
        "open_with_sla": with_sla.count(),
        "breached": breached,
        "due_soon": due_soon,
        "met": met,
        "missed": missed,
        "compliance_rate": compliance,
    }


def ai_accuracy_report(tickets, low_confidence=DEFAULT_LOW_CONFIDENCE):
    """Classifier health: auto-handled rate, override (correction) rate, confidence."""
    classified = tickets.exclude(ai_confidence=None)
    n = classified.count()
    corrections = (
        _audit_qs(tickets).filter(event="correction").values("ticket").distinct().count()
    )
    avg_conf = classified.aggregate(a=Avg("ai_confidence"))["a"]
    uncategorized = classified.filter(sub_topic_ref__isnull=True).count()

    return {
        "classified": n,
        "auto_handled": tickets.filter(ai_handled=True).count(),
        "auto_resolved": tickets.filter(status=Ticket.STATUS_AUTO_RESOLVED).count(),
        "uncategorized": uncategorized,
        "low_confidence": classified.filter(ai_confidence__lt=low_confidence).count(),
        "avg_confidence": round(avg_conf, 4) if avg_conf is not None else None,
        "corrections": corrections,
        "accuracy_rate": round(1 - corrections / n, 4) if n else None,
    }


# Audit events attributable to a human agent.
_AGENT_EVENTS = (
    "reply_sent", "draft_created", "ignored", "unignored", "correction", "decision",
)


def agent_performance_report(tickets):
    """Per-agent activity from the audit log (system/ai actors excluded)."""
    rows = (
        _audit_qs(tickets)
        .exclude(actor__in=["ai", "system"])
        .values_list("actor", "event")
    )
    per_agent = {}
    for actor, event in rows:
        bucket = per_agent.setdefault(actor, Counter())
        bucket[event] += 1
        bucket["total"] += 1
    return {
        actor: {"total": c["total"], **{e: c[e] for e in _AGENT_EVENTS if c[e]}}
        for actor, c in sorted(per_agent.items(), key=lambda kv: -kv[1]["total"])
    }


def pipeline_report(tickets, pending_evidence=0):
    """Workflow buckets that actually move (vs. new/classified which tickets skip).
    `pending_evidence` is the count of held PendingConversations (no ticket yet)."""
    bs = dict(tickets.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    return {
        "waiting_evidence": pending_evidence + bs.get(Ticket.STATUS_AWAITING_EVIDENCE, 0),
        "awaiting_agent": bs.get(Ticket.STATUS_AWAITING_AGENT, 0),
        "in_progress": bs.get(Ticket.STATUS_IN_PROGRESS, 0) + bs.get(Ticket.STATUS_ESCALATED, 0),
        "resolved": (bs.get(Ticket.STATUS_RESOLVED, 0)
                     + bs.get(Ticket.STATUS_AUTO_RESOLVED, 0)),
        "closed": bs.get(Ticket.STATUS_CLOSED, 0),
        "pending_conversations": pending_evidence,
    }


def overview(tickets, now=None, pending_evidence=0, escalations=0):
    """Everything in one payload for a dashboard landing page."""
    return {
        "volume": volume_report(tickets),
        "pipeline": {**pipeline_report(tickets, pending_evidence), "escalation": escalations},
        "sla": sla_report(tickets, now=now),
        "ai": ai_accuracy_report(tickets),
        "agents": agent_performance_report(tickets),
        "escalation": escalations,
    }
