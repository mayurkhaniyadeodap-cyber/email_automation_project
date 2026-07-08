"""
Manager-dashboard reports (ADDITIVE): summary KPI cards, employee performance, scoreboard,
daily trend series, and the manual/auto reply + login-history report rows. Reads the reporting
tables (apps.analytics.models) plus Ticket / Escalation / PendingConversation.
"""

from datetime import timedelta

from django.db.models import Avg, Count, Max
from django.utils import timezone

from apps.tickets.models import (AuditLogEntry, Escalation, InternalEmail, PendingConversation,
                                 Ticket)

from .models import (AutoReplyLog, EmployeeActivity, EmployeeLoginHistory, ManualReplyLog)


def _counts(qs, field, now=None):
    """{total, today, week} for a queryset on a datetime `field`."""
    now = now or timezone.now()
    today = now.date()
    week = now - timedelta(days=7)
    return {
        "total": qs.count(),
        "today": qs.filter(**{f"{field}__date": today}).count(),
        "week": qs.filter(**{f"{field}__gte": week}).count(),
    }


def summary(brand_ids, now=None):
    """The 12 KPI cards -- each {total, today, week}."""
    now = now or timezone.now()
    T = Ticket.objects.filter(brand_id__in=brand_ids)
    E = Escalation.objects.filter(brand_id__in=brand_ids)
    P = PendingConversation.objects.filter(brand_id__in=brand_ids)
    M = ManualReplyLog.objects.filter(brand_id__in=brand_ids)
    A = AutoReplyLog.objects.filter(brand_id__in=brand_ids)
    msgs_inbound = T.filter(messages__direction="inbound").distinct()

    return {
        "total_emails": {"total": msgs_inbound.count() + E.count(),
                         **_dual(msgs_inbound, E, "created_at", now)},
        "total_tickets": _counts(T, "created_at", now),
        "open_tickets": _open_counts(T.filter(is_ignored=False)
                                     .exclude(status__in=Ticket.TERMINAL_STATUSES)),
        "closed_tickets": _counts(T.filter(status=Ticket.STATUS_CLOSED), "updated_at", now),
        "ignored_emails": _open_counts(T.filter(is_ignored=True)),
        "escalations": _counts(E.exclude(status__in=Escalation.TERMINAL_STATUSES), "created_at", now),
        "high_priority": _open_counts(T.filter(is_ignored=False, priority=Ticket.PRIORITY_HIGH)
                                      .exclude(status__in=Ticket.TERMINAL_STATUSES)),
        "internal": _open_counts(InternalEmail.objects.filter(brand_id__in=brand_ids)
                                 .exclude(status=InternalEmail.STATUS_DELETED)),
        "auto_replies": _counts(A, "created_at", now),
        "manual_replies": _counts(M, "created_at", now),
        "pending_manual_review": _open_counts(E.filter(status=Escalation.STATUS_MANUAL_REVIEW)),
        "awaiting_customer_reply": _open_counts(E.filter(status=Escalation.STATUS_AWAITING_REPLY)),
        "awaiting_evidence": _open_counts(
            P.filter(status__in=["awaiting_evidence", "waiting_for_video"])),
        # Automation-overview metrics (additive): verification-held conversations, and conversations
        # that have received at least one evidence request. Distinct from the pipeline-state counts.
        "verification_emails": _open_counts(P.filter(status="awaiting_verification")),
        "evidence_requests": {"total": P.filter(evidence_requests__gt=0).count()},
        "resolved_today": {"total": T.filter(status__in=[Ticket.STATUS_RESOLVED,
                                                         Ticket.STATUS_AUTO_RESOLVED]).count(),
                           "today": T.filter(resolved_at__date=now.date()).count(),
                           "week": T.filter(resolved_at__gte=now - timedelta(days=7)).count()},
    }


def internal_metrics(brand_ids, now=None):
    """Internal Communications reporting metrics (independent of the support pipeline)."""
    now = now or timezone.now()
    IE = InternalEmail.objects.filter(brand_id__in=brand_ids).exclude(
        status=InternalEmail.STATUS_DELETED)
    # employee replies + average first-response time (received -> first outbound), computed in
    # Python since SQLite has no JSON `contains` lookup.
    from datetime import datetime
    replied, durations = 0, []
    for ie in IE:
        out = next((c for c in (ie.conversation or []) if c.get("direction") == "outbound"), None)
        if not out:
            continue
        replied += 1
        if ie.received_at and out.get("at"):
            try:
                durations.append((datetime.fromisoformat(out["at"]) - ie.received_at).total_seconds())
            except (ValueError, TypeError):
                pass
    avg = round(sum(durations) / len(durations)) if durations else None
    return {
        "today": IE.filter(created_at__date=now.date()).count(),
        "total": IE.count(),
        "employee_replies": replied,
        "archived": IE.filter(status=InternalEmail.STATUS_ARCHIVED).count(),
        "pending": IE.filter(status=InternalEmail.STATUS_INTERNAL_REVIEW).count(),
        "avg_response_seconds": avg,
    }


def _open_counts(qs):
    return {"total": qs.count(), "today": None, "week": None}


def _dual(a, b, field, now):
    today = now.date(); week = now - timedelta(days=7)
    return {"today": a.filter(**{f"{field}__date": today}).count()
                     + b.filter(**{f"{field}__date": today}).count(),
            "week": a.filter(**{f"{field}__gte": week}).count()
                    + b.filter(**{f"{field}__gte": week}).count()}


def daily_series(brand_ids, days=7, now=None):
    """Per-day counts for the trend charts (last `days` days)."""
    now = now or timezone.now()
    start = (now - timedelta(days=days - 1)).date()
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    def by_day(qs, field):
        rows = dict((d.isoformat() if hasattr(d, "isoformat") else d, n) for d, n in (
            qs.filter(**{f"{field}__date__gte": start})
              .values_list(f"{field}__date").annotate(n=Count("id"))))
        return [rows.get(lbl, 0) for lbl in labels]

    return {
        "labels": labels,
        "emails_received": by_day(Ticket.objects.filter(brand_id__in=brand_ids), "created_at"),
        "tickets_created": by_day(Ticket.objects.filter(brand_id__in=brand_ids), "created_at"),
        "tickets_resolved": by_day(Ticket.objects.filter(brand_id__in=brand_ids), "resolved_at"),
        "auto_replies": by_day(AutoReplyLog.objects.filter(brand_id__in=brand_ids), "created_at"),
        "manual_replies": by_day(ManualReplyLog.objects.filter(brand_id__in=brand_ids), "created_at"),
        "escalations": by_day(Escalation.objects.filter(brand_id__in=brand_ids), "created_at"),
    }


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def ticket_trend(brand_ids, rng="week", now=None):
    """Ticket-created counts for the dynamic Ticket Trend chart. Returns {labels, values}.
      week  -> last 7 days,   labels = weekday   (Mon..Sun)
      month -> last 30 days,  labels = day-of-month (1..31)
      year  -> last 12 months, labels = month    (Jan..Dec)
    One grouped query per range (optimized for large datasets)."""
    now = now or timezone.now()
    T = Ticket.objects.filter(brand_id__in=brand_ids)

    if rng == "year":
        from django.db.models.functions import TruncMonth

        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months, y, m = [], first.year, first.month
        for _ in range(12):
            months.append((y, m))
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        months.reverse()
        cutoff = first.replace(year=months[0][0], month=months[0][1])
        by = {}
        for row in (T.filter(created_at__gte=cutoff).annotate(mo=TruncMonth("created_at"))
                    .values("mo").annotate(n=Count("id"))):
            mo = row["mo"]
            if mo:
                by[(mo.year, mo.month)] = row["n"]
        return {"labels": [_MONTH_ABBR[mm - 1] for (_, mm) in months],
                "values": [by.get((yy, mm), 0) for (yy, mm) in months]}

    days = 30 if rng == "month" else 7
    start = (now - timedelta(days=days - 1)).date()
    rows = dict(
        (d.isoformat() if hasattr(d, "isoformat") else str(d), n)
        for d, n in (T.filter(created_at__date__gte=start)
                     .values_list("created_at__date").annotate(n=Count("id"))))
    dates = [start + timedelta(days=i) for i in range(days)]
    labels = [(d.strftime("%a") if rng == "week" else str(d.day)) for d in dates]
    values = [rows.get(d.isoformat(), 0) for d in dates]
    return {"labels": labels, "values": values}


def recent_activity(brand_ids, limit=20):
    """The latest audit events across the brand's tickets (newest first) for the dashboard's
    Recent Activity timeline. Additive/read-only -- reuses the existing AuditLogEntry rows."""
    rows = (AuditLogEntry.objects.filter(ticket__brand_id__in=brand_ids)
            .select_related("ticket").order_by("-created_at")[:limit])
    out = []
    for r in rows:
        t = r.ticket
        out.append({
            "at": r.created_at.isoformat(),
            "event": r.event,
            "actor": r.actor,
            "ticket": (getattr(t, "ticket_number", "") or getattr(t, "ticket_id", "")) if t else "",
            "detail": r.detail or {},
        })
    return out


def category_distribution(brand_ids):
    rows = (Ticket.objects.filter(brand_id__in=brand_ids).exclude(category="")
            .values_list("category").annotate(n=Count("id")).order_by("-n")[:12])
    return [{"label": c, "value": n} for c, n in rows]


def _alias_owner_map(brand_ids):
    """{alias_email_lower: owner_name} for support emails that name an owner. Lets replies sent
    from any of a person's aliases be credited to that ONE employee."""
    try:
        from apps.brand_settings.models import SupportEmail
        return {(e or "").lower(): o for e, o in SupportEmail.objects.filter(
            brand_id__in=brand_ids).exclude(owner_name="").values_list("email", "owner_name") if e}
    except Exception:  # noqa: BLE001
        return {}


def employee_performance(brand_ids):
    """Per-employee KPIs: manual replies, auto triggered, tickets created/resolved, escalations
    handled, avg response time, last active. Manual replies are credited to the OWNER of the
    sender alias (Settings -> Support Emails) when set, so a person's aliases count together."""
    by_email = {}
    owner_map = _alias_owner_map(brand_ids)

    def row(key, name="", email=None):
        r = by_email.setdefault(key or "(unknown)", {
            "employee_key": key or "(unknown)",   # the identity used to FILTER the report
            "employee_email": key if email is None else email, "employee_name": name,
            "manual_replies": 0, "auto_replies": 0, "tickets_created": 0, "tickets_resolved": 0,
            "escalations_handled": 0, "avg_response_seconds": None, "last_active": None})
        if name and not r["employee_name"]:
            r["employee_name"] = name
        if email:
            r["employee_email"] = email
        return r

    # Credit each reply to the alias OWNER when known (group a person's aliases together), else the
    # login employee. The row KEY (and report filter) is that attributed identity. Aggregate count
    # + weighted avg response in Python.
    mr = (ManualReplyLog.objects.filter(brand_id__in=brand_ids)
          .values("employee_email", "employee_name", "sender_email")
          .annotate(n=Count("id"), avg=Avg("response_seconds"), last=Max("created_at")))
    agg = {}
    for m in mr:
        owner = owner_map.get((m["sender_email"] or "").lower())
        key = owner or m["employee_email"] or "(unknown)"
        a = agg.setdefault(key, {"name": owner or m["employee_name"],
                                 "email": m["sender_email"] if owner else m["employee_email"],
                                 "n": 0, "wsum": 0.0, "wcnt": 0, "last": None})
        a["n"] += m["n"]
        if m["avg"] is not None:
            a["wsum"] += m["avg"] * m["n"]; a["wcnt"] += m["n"]
        if m["last"] and (a["last"] is None or m["last"] > a["last"]):
            a["last"] = m["last"]
    for key, a in agg.items():
        # owner rows show the alias as the email; login rows show the login email.
        r = row(key, a["name"], email=a["email"])
        r["manual_replies"] = a["n"]
        r["avg_response_seconds"] = round(a["wsum"] / a["wcnt"]) if a["wcnt"] else None
        # Last Active: real session activity (set below) for login users; for owner rows -- who
        # have no login session -- fall back to their most recent reply time.
        if a["last"]:
            r["last_active"] = a["last"].isoformat()

    # tickets created / resolved + escalations handled, from audit + escalation resolver.
    from apps.tickets.models import AuditLogEntry
    created = (AuditLogEntry.objects.filter(ticket__brand_id__in=brand_ids, event="ticket_created")
               .exclude(actor__in=["system", "ai"]).values_list("actor")
               .annotate(n=Count("id")))
    for actor, n in created:
        row(actor)["tickets_created"] = n
    resolved = (Ticket.objects.filter(brand_id__in=brand_ids,
                                      status__in=[Ticket.STATUS_RESOLVED, Ticket.STATUS_CLOSED])
                .exclude(resolved_by="").values_list("resolved_by").annotate(n=Count("id"))
                if _ticket_has_resolved_by() else [])
    for actor, n in resolved:
        row(actor)["tickets_resolved"] = n
    esc = (Escalation.objects.filter(brand_id__in=brand_ids).exclude(resolved_by="")
           .values_list("resolved_by").annotate(n=Count("id")))
    for actor, n in esc:
        row(actor)["escalations_handled"] = n

    for act in EmployeeActivity.objects.select_related("employee"):
        email = getattr(act.employee, "email", "")
        if email in by_email and act.last_active_at:
            by_email[email]["last_active"] = act.last_active_at.isoformat()

    return sorted(by_email.values(), key=lambda r: -r["manual_replies"])


def _ticket_has_resolved_by():
    return any(f.name == "resolved_by" for f in Ticket._meta.get_fields())


def scoreboard(brand_ids, now=None):
    """Today's leaders: most manual replies, fastest response, most tickets/escalations."""
    now = now or timezone.now()
    today = now.date()
    mr = ManualReplyLog.objects.filter(brand_id__in=brand_ids, created_at__date=today)
    most = (mr.values("employee_name", "employee_email").annotate(n=Count("id")).order_by("-n").first())
    fastest = (mr.exclude(response_seconds=None).values("employee_name", "employee_email")
               .annotate(avg=Avg("response_seconds")).order_by("avg").first())
    perf = employee_performance(brand_ids)
    top_resolved = max(perf, key=lambda r: r["tickets_resolved"], default=None)
    top_esc = max(perf, key=lambda r: r["escalations_handled"], default=None)
    return {
        "most_manual_replies": most,
        "fastest_response": fastest,
        "most_tickets_resolved": top_resolved and {"employee_name": top_resolved["employee_name"],
                                                    "n": top_resolved["tickets_resolved"]},
        "most_escalations_handled": top_esc and {"employee_name": top_esc["employee_name"],
                                                  "n": top_esc["escalations_handled"]},
    }


# --- report rows (with date filtering) ---------------------------------------------------------
def _window(qs, field, since=None, until=None):
    if since:
        qs = qs.filter(**{f"{field}__date__gte": since})
    if until:
        qs = qs.filter(**{f"{field}__date__lte": until})
    return qs


def manual_reply_rows(brand_ids, since=None, until=None, employee=None):
    qs = _window(ManualReplyLog.objects.filter(brand_id__in=brand_ids), "created_at", since, until)
    owner_map = _alias_owner_map(brand_ids)
    out = []
    for r in qs:
        owner = owner_map.get((r.sender_email or "").lower())
        attributed = owner or r.employee_email      # owner of the alias, else the login employee
        if employee and attributed != employee:      # filter matches the Employee Performance key
            continue
        out.append({"date": r.created_at.isoformat(),
                    "employee_name": owner or r.employee_name,
                    # owner-attributed replies show the owner's alias, not the shared login email.
                    "employee_email": (r.sender_email if owner else r.employee_email),
                    "sender_email": r.sender_email,
                    "customer": r.customer_email,
                    "subject": r.subject, "ticket": r.ticket_ref,
                    "reply_time": r.created_at.isoformat(),
                    "attachments": r.attachments, "status": r.status,
                    "ticket_pk": r.ticket_id, "escalation_id": r.escalation_id})
    return out


def auto_reply_rows(brand_ids, since=None, until=None):
    qs = _window(AutoReplyLog.objects.filter(brand_id__in=brand_ids), "created_at", since, until)
    return [{"date": r.created_at.isoformat(), "customer": r.customer_email, "subject": r.subject,
             "template": r.template, "trigger": r.trigger, "auto_reply_time": r.created_at.isoformat(),
             "ticket": r.ticket_ref, "status": r.status, "ticket_pk": r.ticket_id} for r in qs]


def login_history_rows(brand_ids=None, since=None, until=None):
    qs = _window(EmployeeLoginHistory.objects.select_related("employee"), "login_at", since, until)
    out = []
    for r in qs:
        out.append({"employee": getattr(r.employee, "email", ""),
                    "login_at": r.login_at and r.login_at.isoformat(),
                    "logout_at": r.logout_at and r.logout_at.isoformat(),
                    "session_seconds": r.session_seconds, "ip_address": r.ip_address,
                    "device": r.device, "browser": r.browser})
    return out
