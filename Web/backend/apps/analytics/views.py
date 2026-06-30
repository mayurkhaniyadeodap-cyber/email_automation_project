"""
Analytics endpoints (doc section 13, Phase 6). All reports are scoped to the
caller's organizations and accept the same ?organization= / ?brand= dropdowns as
the rest of the API, plus an optional ?since=YYYY-MM-DD window.

    GET /api/analytics/overview/      -> volume + sla + ai + agents
    GET /api/analytics/volume/
    GET /api/analytics/sla/
    GET /api/analytics/ai-accuracy/
    GET /api/analytics/agents/
"""

import logging
from datetime import datetime

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes  # type: ignore[import]
from rest_framework.permissions import IsAuthenticated  # type: ignore[import]
from rest_framework.response import Response  # type: ignore[import]

from apps.organizations.models import Brand
from apps.tickets.models import Escalation, PendingConversation, Ticket

from . import dashboard as dash, exports, reports

logger = logging.getLogger(__name__)


def _scoped_brand_ids(request):
    """Brand ids the caller may report on, narrowed by ?organization / ?brand."""
    qs = Brand.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(organization__in=request.user.organizations.all())
    org = request.query_params.get("organization")
    if org:
        qs = qs.filter(organization=org)
    brand = request.query_params.get("brand")
    if brand:
        qs = qs.filter(id=brand)
    return list(qs.values_list("id", flat=True))


def _date_window(request):
    """(since, until) dates from ?range=today|yesterday|7d|30d or ?since=&until=."""
    from datetime import date, timedelta
    rng = request.query_params.get("range")
    today = timezone.now().date()
    if rng == "today":
        return today, today
    if rng == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if rng == "7d":
        return today - timedelta(days=6), today
    if rng == "30d":
        return today - timedelta(days=29), today

    def _d(v):
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            return None
    return _d(request.query_params.get("since")), _d(request.query_params.get("until"))


def _scope(qs, request):
    """Narrow a queryset by the caller's orgs + org/brand query params."""
    user = request.user
    if not user.is_superuser:
        qs = qs.filter(organization__in=user.organizations.all())
    org = request.query_params.get("organization")
    if org:
        qs = qs.filter(organization=org)
    brand = request.query_params.get("brand")
    if brand:
        qs = qs.filter(brand=brand)
    return qs


def scoped_tickets(request):
    """Tickets the caller may see, narrowed by org/brand/since query params."""
    qs = _scope(Ticket.objects.all(), request)
    since = request.query_params.get("since")
    if since:
        try:
            day = datetime.strptime(since, "%Y-%m-%d").date()
            qs = qs.filter(created_at__date__gte=day)
        except ValueError:
            pass
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def overview(request):
    # Held conversations genuinely WAITING for evidence/video (no ticket yet). Exclude closed
    # (finished inquiries / promoted) and identity-verification holds -- they are not waiting
    # for evidence, so they must not inflate the "Waiting for Evidence / Video" bucket.
    pending = _scope(
        PendingConversation.objects.filter(
            status__in=["awaiting_evidence", "waiting_for_video"]),
        request).count()
    # HIGH-priority manual-review escalation queue (legal / consumer-court / grievance).
    escalations = _scope(
        Escalation.objects.exclude(status__in=Escalation.TERMINAL_STATUSES), request).count()
    return Response(reports.overview(scoped_tickets(request), pending_evidence=pending,
                                     escalations=escalations))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def volume(request):
    return Response(reports.volume_report(scoped_tickets(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sla(request):
    return Response(reports.sla_report(scoped_tickets(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ai_accuracy(request):
    return Response(reports.ai_accuracy_report(scoped_tickets(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def agents(request):
    return Response(reports.agent_performance_report(scoped_tickets(request)))


# === Manager dashboard + reports (additive) ====================================================
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def manager_dashboard(request):
    """All KPIs for the redesigned manager dashboard: 12 summary cards, pipeline, trend series,
    distributions, employee performance, and the scoreboard."""
    bids = _scoped_brand_ids(request)
    logger.info("DASHBOARD_VIEWED user=%s brands=%s", request.user.get_username(), len(bids))
    pending = _scope(PendingConversation.objects.filter(
        status__in=["awaiting_evidence", "waiting_for_video"]), request).count()
    escalations = _scope(
        Escalation.objects.exclude(status__in=Escalation.TERMINAL_STATUSES), request).count()
    return Response({
        "summary": dash.summary(bids),
        "pipeline": {**reports.pipeline_report(scoped_tickets(request), pending),
                     "escalation": escalations,
                     "awaiting_customer_reply": _scope(Escalation.objects.filter(
                         status=Escalation.STATUS_AWAITING_REPLY), request).count(),
                     "manual_review": _scope(Escalation.objects.filter(
                         status=Escalation.STATUS_MANUAL_REVIEW), request).count()},
        "series": dash.daily_series(bids, days=7),
        "category_distribution": dash.category_distribution(bids),
        "employee_performance": dash.employee_performance(bids),
        "scoreboard": dash.scoreboard(bids),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def employee_performance(request):
    return Response(dash.employee_performance(_scoped_brand_ids(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def internal_metrics(request):
    """Internal Communications metrics: today / total / employee replies / archived / pending /
    avg response time. Independent of the customer-support pipeline."""
    return Response(dash.internal_metrics(_scoped_brand_ids(request)))


def _report(request, rows_fn, *, filename, title, **extra):
    bids = _scoped_brand_ids(request)
    since, until = _date_window(request)
    rows = rows_fn(bids, since=since, until=until, **extra)
    fmt = request.query_params.get("export")
    if fmt:
        logger.info("REPORT_GENERATED report=%s format=%s rows=%d user=%s",
                    filename, fmt, len(rows), request.user.get_username())
        return exports.export(fmt, rows, filename=filename, title=title)
    return Response({"count": len(rows), "results": rows})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def manual_reply_report(request):
    return _report(request, dash.manual_reply_rows,
                   filename="manual-reply-report", title="Manual Reply Report",
                   employee=request.query_params.get("employee") or None)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def auto_reply_report(request):
    return _report(request, dash.auto_reply_rows,
                   filename="auto-reply-report", title="Auto Reply Report")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def login_history(request):
    since, until = _date_window(request)
    rows = dash.login_history_rows(since=since, until=until)
    fmt = request.query_params.get("export")
    if fmt:
        logger.info("REPORT_GENERATED report=login-history format=%s rows=%d", fmt, len(rows))
        return exports.export(fmt, rows, filename="employee-login-history",
                              title="Employee Login History")
    return Response({"count": len(rows), "results": rows})
