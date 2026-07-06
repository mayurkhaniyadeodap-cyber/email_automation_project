"""Global search autocomplete.

ONE endpoint that powers every search box via the shared <SearchAutocomplete> component. It
suggests matches across every searchable field -- customer name, email, subject, order id, phone,
ticket number, tracking/AWB, category, sub-category, status, priority, assignee, company.

Contract (matches the feature spec):
  GET /api/search/suggest/?q=<term>[&types=customer,email,...][&organization=&brand=]
  - returns {"suggestions": [{"value": "...", "type": "customer"}, ...]}
  - min 2 chars (shorter -> empty), case-insensitive, <= 10 suggestions, prefix matches ranked first.
Every source is org-scoped to the requesting user and individually guarded, so a missing/JSON field
can never 500 the suggest box.
"""
from rest_framework.decorators import api_view, permission_classes  # type: ignore
from rest_framework.permissions import IsAuthenticated  # type: ignore
from rest_framework.response import Response  # type: ignore

MIN_CHARS = 2
MAX_SUGGESTIONS = 10
_PER_SOURCE_CAP = 25   # rows pulled per source before global ranking

# Human-readable status / priority labels, matched case-insensitively against the query.
_STATUS_LABELS = ["New", "Classified", "Awaiting Agent", "In Progress", "Awaiting Evidence",
                  "Awaiting Customer Reply", "Waiting For Video", "Escalated", "Resolved",
                  "Closed", "Ignored", "Held", "Awaiting Reply", "Open", "Sent", "Draft"]
_PRIORITY_LABELS = ["Low", "Normal", "Medium", "High", "Urgent"]


def _scope(qs, request, org_field="organization", brand_field="brand"):
    """Restrict a queryset to the user's organizations (+ optional ?organization=&brand=)."""
    user = request.user
    if not getattr(user, "is_superuser", False):
        qs = qs.filter(**{f"{org_field}__in": user.organizations.all()})
    org = request.query_params.get("organization")
    brand = request.query_params.get("brand")
    if org:
        qs = qs.filter(**{org_field: org})
    if brand:
        qs = qs.filter(**{brand_field: brand})
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_suggest(request):
    q = (request.query_params.get("q") or "").strip()
    if len(q) < MIN_CHARS:
        return Response({"suggestions": []})
    low = q.lower()

    from apps.organizations.models import Brand, Organization
    from apps.taxonomy.models import Category, SubTopic
    from apps.tickets.models import (Escalation, InternalEmail, PendingConversation, Ticket)

    seen, out = set(), []

    def add(value, typ):
        value = ("" if value is None else str(value)).strip()
        if not value:
            return
        key = (typ, value.lower())
        if key in seen:
            return
        seen.add(key)
        out.append({"value": value, "type": typ})

    def src(build_qs, field, typ):
        """Pull DISTINCT icontains matches for `field` from a (lazily built) queryset. Fully
        guarded: a missing field / JSON-lookup quirk is skipped, never raised."""
        try:
            qs = build_qs()
            rows = (qs.filter(**{f"{field}__icontains": q})
                      .values_list(field, flat=True).distinct()[:_PER_SOURCE_CAP])
            for v in rows:
                add(v, typ)
        except Exception:  # noqa: BLE001 -- suggestions are best-effort; never fail the request
            pass

    tickets = lambda: _scope(Ticket.objects.all(), request)          # noqa: E731
    pendings = lambda: _scope(PendingConversation.objects.all(), request)  # noqa: E731

    # --- Tickets: the richest source (customer identity + concern + order/tracking) ---
    src(tickets, "extracted__customer_name", "customer")
    src(tickets, "customer_email", "email")
    src(tickets, "subject", "subject")
    src(tickets, "ticket_number", "ticket")
    src(tickets, "extracted__order_id", "order")
    src(tickets, "extracted__phone", "phone")
    src(tickets, "extracted__awb", "tracking")
    src(tickets, "sub_topic", "subtopic")
    src(tickets, "category", "category")
    # --- Held conversations (Inbox 'Held -- no ticket') ---
    src(pendings, "customer_email", "email")
    src(pendings, "subject", "subject")
    src(pendings, "order_id", "order")
    src(pendings, "phone", "phone")
    # --- Taxonomy names, e.g. "Missing Item" / "Damaged Item" ---
    src(lambda: SubTopic.objects.all(), "name", "subtopic")
    src(lambda: Category.objects.all(), "name", "category")
    # --- Assignees ---
    src(lambda: _scope(Escalation.objects.all(), request), "assigned_to", "assignee")
    src(lambda: _scope(InternalEmail.objects.all(), request), "assigned_to", "assignee")
    # --- Company / brand ---
    src(lambda: Organization.objects.all(), "name", "company")
    src(lambda: _scope(Brand.objects.all(), request), "name", "company")
    # --- Status / priority (static enums) ---
    for s in _STATUS_LABELS:
        if low in s.lower():
            add(s, "status")
    for p in _PRIORITY_LABELS:
        if low in p.lower():
            add(p, "priority")

    # Optional client filter: ?types=customer,email,subject
    types = request.query_params.get("types")
    if types:
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        out = [s for s in out if s["type"] in wanted]

    # Rank: prefix matches first, then shorter values, then alphabetical.
    out.sort(key=lambda s: (0 if s["value"].lower().startswith(low) else 1,
                            len(s["value"]), s["value"].lower()))
    return Response({"suggestions": out[:MAX_SUGGESTIONS]})
