"""
Decision engine (doc sections 5 & 6) -- the IF/THEN/Action playbook adapted for mail.

Once a ticket is classified, the engine picks the matching rule for its sub-topic and
maps the rule's Action to a mail behavior in Hybrid mode:

    Info only                 -> auto-send the THEN response in-thread     (Auto-Resolved)
    Await evidence            -> auto-send the evidence-request template    (Awaiting Evidence)
    Create Ticket             -> draft a reply for an agent to approve      (Awaiting Agent)
    Update in system          -> agent task (data changes are never auto)   (Awaiting Agent)
    Continue to next check    -> needs live data (Phase 5) -> draft + flag  (Awaiting Agent)
    Trigger cancel/refund/...  -> high-attention agent ticket               (Escalated)

Guardrails (doc section 6) run BEFORE any auto-send and force a draft / human:
    confidence below the brand threshold, sensitive sub-topics, missing mandatory
    inputs (ask instead of guessing), conditions that need live data, and angry mail.

Condition evaluation is tri-state: True / False / None(=unevaluable). Conditions that
depend on live Shopify/Shipping data return None here and are wired up in Phase 5 by
passing those facts in `context`; until then such rules cause a safe draft, never a
wrong auto-answer.
"""

import logging
import re
from dataclasses import dataclass, field

from django.utils import timezone

from apps.brand_settings.models import BrandSettings
from apps.taxonomy.models import Rule, Template
from apps.tickets.models import AuditLogEntry, Message, Ticket

from . import templates

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_HOLDING_REPLY = "Our team will review and get back to you shortly."
DEFAULT_EVIDENCE_REQUEST = (
    "To help us resolve this quickly, please reply with:\n"
    "  1. Product images\n"
    "  2. Damage images (if any)\n"
    "  3. A product / unboxing video\n"
    "  4. Package images (box + shipping label)\n"
    "Once we receive these we'll process your request right away."
)

# Default first-response SLA targets (minutes) by priority (doc section 12).
DEFAULT_SLA_MINUTES = {
    Ticket.PRIORITY_HIGH: 120,
    Ticket.PRIORITY_NORMAL: 24 * 60,
    Ticket.PRIORITY_LOW: None,
}

# Send modes.
AUTO = "auto"
DRAFT = "draft"
NONE = "none"

# Action router (Mail Flow v2.0 §4) — the four routes every classified mail lands in.
ROUTE_LABELS = {
    "A": "Auto-answer & close",
    "B": "Evidence -> ticket",
    "C": "Direct ticket",
    "D": "Human first",
}


@dataclass
class DecisionPlan:
    action_code: str                 # Rule action or synthetic (holding/evidence_request/...)
    action_label: str                # human label stored on ticket.action_taken
    send_mode: str = NONE            # auto | draft | none
    reply_text: str = ""
    status: str = Ticket.STATUS_AWAITING_AGENT
    priority: str = Ticket.PRIORITY_NORMAL
    ai_handled: bool = False
    create_agent_task: bool = False
    reasons: list = field(default_factory=list)
    rule_id: int | None = None


def route_for(ticket, plan):
    """Map a ticket + its DecisionPlan to one of the four routes (A/B/C/D).

    A — auto-answered from playbook/APIs and closed (no ticket).
    B — needs photo/video evidence before a ticket (damage / wrong / missing / refund).
    C — a direct ticket (delay / EDD / address change / RTO / POD): mandatory fields only.
    D — human-first: sensitive, escalated or low-confidence -> draft + agent.
    """
    from apps.taxonomy.models import Rule

    if plan.ai_handled and plan.status == Ticket.STATUS_AUTO_RESOLVED:
        return "A", ROUTE_LABELS["A"]

    reasons = set(plan.reasons or [])
    sub = ticket.sub_topic_ref
    if (plan.status == Ticket.STATUS_ESCALATED
            or (sub is not None and getattr(sub, "is_sensitive", False))
            or reasons & {"low_confidence", "sensitive_subtopic", "requires_agent"}):
        return "D", ROUTE_LABELS["D"]

    cat = ticket.category_ref
    if (plan.action_code in (Rule.ACTION_AWAIT_EVIDENCE, "evidence_request")
            or "requires_evidence" in reasons
            or (sub is not None and getattr(sub, "requires_evidence", False))
            or (sub is not None and getattr(sub, "requires_video", False))
            or (cat is not None and getattr(cat, "requires_video", False))):
        return "B", ROUTE_LABELS["B"]

    return "C", ROUTE_LABELS["C"]


# --------------------------------------------------------------------------- #
# Settings + context helpers
# --------------------------------------------------------------------------- #

def _settings_for(brand):
    try:
        return brand.settings
    except BrandSettings.DoesNotExist:
        return None


def _toggle(settings, action):
    """Per-action automation toggle: 'auto_send' | 'draft' | 'off' | None."""
    if not settings or not settings.automation_toggles:
        return None
    return settings.automation_toggles.get(action)


def _has_evidence(context):
    return bool(context.get("has_unboxing_video") or context.get("has_photo")
                or context.get("has_video"))


def _ticket_has_evidence_attachment(ticket):
    """True if the ticket already has a stored photo/video attachment (MIME or extension).
    The authoritative 'evidence present' check -- independent of the ctx flags, which can be
    missing if a flag-sync step didn't run."""
    from apps.ingestion import evidence as _ev

    try:
        return any(_ev.is_photo(a.filename, a.content_type)
                   or _ev.is_video(a.filename, a.content_type)
                   for a in ticket.attachments.all())
    except Exception:  # noqa: BLE001 -- decisioning must never crash on this
        return False


def _evidence_required(ticket, sub, ctx):
    """Whether this case GENUINELY needs photo/video evidence -- independent of a stray
    AI `requires_evidence` flag.

    The AI sometimes sets requires_evidence on info categories where it makes no sense
    (e.g. it set it on a Shipment-Tracking email -> the case was wrongly pushed into the
    evidence -> ticket flow, bypassing the auto-reply policy). We trust the sub-topic's
    explicit DB flags and the deterministic category-first evidence policy instead, which
    returns 'none' for tracking / offers / info categories."""
    try:
        from apps.ingestion import evidence
    except Exception:  # noqa: BLE001 -- fail safe: keep historical behavior
        return bool(getattr(sub, "requires_evidence", False)
                    or getattr(sub, "requires_video", False))
    text = " ".join(filter(None, [
        ticket.subject or "", ticket.issue_summary or "", str(ctx.get("issue_summary") or ""),
        ticket.sub_topic or "", ticket.category or "",
    ]))
    # "Order Shown Delivered But Not Received": a non-delivery dispute -> an unboxing video is
    # IMPOSSIBLE. Never require evidence, even when the sub-topic's DB flag says so (the flag
    # check below would otherwise force a needless unbox-video request -- the reported bug).
    if evidence.is_delivered_not_received(text):
        logger.info("EVIDENCE-NOT-REQUIRED delivered_not_received -> no photo/video.")
        return False
    if getattr(sub, "requires_evidence", False) or getattr(sub, "requires_video", False):
        return True
    level = evidence.evidence_level(
        category=ticket.category or "", sub_topic=ticket.sub_topic or "",
        issue_summary=str(ctx.get("issue_summary") or ""), text=text,
        category_ref=ticket.category_ref, sub_topic_ref=sub,
    )
    return evidence.requires_evidence(level)


# --------------------------------------------------------------------------- #
# Condition evaluation (tri-state) + rule selection
# --------------------------------------------------------------------------- #

# Live-data fact keys filled by the integrations layer (Phase 5). A clause whose
# fact is absent from the context is unevaluable -> the engine drafts.
def _fact(context, key, negated):
    value = context.get(key)
    if value is None:
        return None
    value = bool(value)
    return (not value) if negated else value


def _eval_clause(c, context):
    """Evaluate one AND/OR clause to True / False / None(unevaluable)."""
    c = c.strip().lower()
    if not c or c == "always" or c.startswith("any "):
        return True

    # Evidence-based conditions (from classifier-extracted fields). Check the
    # NEGATIVE form first -- "No ... evidence present" contains "evidence present".
    if (
        "no unboxing" in c
        or "no evidence" in c
        or ("no " in c and "evidence" in c)
        or ("video" in c and "not" in c)
    ):
        return not _has_evidence(context)
    if "evidence present" in c or ("evidence" in c and "present" in c):
        return _has_evidence(context)

    # Live order / shipping / payment facts (Phase 5). Negation is matched
    # ADJACENT to the fact keyword so an unrelated "not" elsewhere in the clause
    # (e.g. "...customer reports not received") doesn't flip the wrong fact.
    if "dispatch" in c:
        return _fact(context, "dispatched", "not dispatch" in c or "undispatched" in c)
    if "delivered" in c:
        return _fact(context, "delivered", "not delivered" in c)
    if "shipped" in c:
        return _fact(context, "shipped", "not shipped" in c or "unshipped" in c)
    if "edd" in c or "breached" in c or "expected delivery" in c:
        return _fact(context, "edd_breached",
                     "not breached" in c or "edd not" in c or "not edd" in c)
    if "custom item" in c or "custom-item" in c:
        return _fact(context, "custom_item", "not a custom" in c or "not custom" in c)
    if "double" in c or "extra payment" in c or "charged twice" in c:
        return _fact(context, "double_payment", False)

    # Unknown free-text condition: don't guess.
    return None


def _combine(results, op):
    if op == "or":
        if any(r is True for r in results):
            return True
        if any(r is None for r in results):
            return None
        return False
    # AND
    if any(r is False for r in results):
        return False
    if any(r is None for r in results):
        return None
    return True


def evaluate_condition(condition, context):
    """Return True / False / None(unevaluable) for a rule's IF condition.

    Conditions are split into AND/OR clauses; each clause is matched to evidence
    (classifier) or live-data facts (integrations). A clause needing a fact that
    isn't in `context` is unevaluable, which makes the whole condition unevaluable
    so the engine safely drafts rather than auto-answering on stale data.
    """
    c = (condition or "").strip().lower()
    if not c or c == "always" or c.startswith("any "):
        return True

    if re.search(r"\bor\b", c):
        parts, op = re.split(r"\bor\b", c), "or"
    else:
        parts, op = re.split(r"\band\b", c), "and"
    results = [_eval_clause(p, context) for p in parts if p.strip()]
    if not results:
        return True
    return _combine(results, op)


def select_rule(sub_topic, context):
    """First active rule (by position) whose condition is True.

    Returns (rule, failure_reason). failure_reason is 'needs_live_data' when some
    rule could not be evaluated (live data required), else 'no_matching_rule'.
    """
    saw_unevaluable = False
    for rule in sub_topic.rules.filter(is_active=True).order_by("position"):
        verdict = evaluate_condition(rule.condition, context)
        if verdict is True:
            return rule, None
        if verdict is None:
            saw_unevaluable = True
    return None, ("needs_live_data" if saw_unevaluable else "no_matching_rule")


# --------------------------------------------------------------------------- #
# Reply-text helpers
# --------------------------------------------------------------------------- #

def _template_body(sub_topic):
    tpl = sub_topic.templates.filter(is_active=True, name="default").first()
    if tpl is None:
        tpl = sub_topic.templates.filter(is_active=True).first()
    return tpl.body if tpl else ""


def _holding_text(settings):
    return (settings.holding_reply if settings and settings.holding_reply
            else DEFAULT_HOLDING_REPLY)


# --------------------------------------------------------------------------- #
# Priority + SLA
# --------------------------------------------------------------------------- #

def _priority_for(sub_topic, action):
    if action == Rule.ACTION_INFO_ONLY:
        return Ticket.PRIORITY_LOW
    if sub_topic.is_sensitive or action == Rule.ACTION_TRIGGER_CRP:
        return Ticket.PRIORITY_HIGH
    # HIGH per doc section 12: delivered-not-received, payment/refund disputes.
    if sub_topic.code == "3.1" or sub_topic.category.code == "8":
        return Ticket.PRIORITY_HIGH
    return Ticket.PRIORITY_NORMAL


def _sla_minutes(settings, sub_topic, priority):
    if settings and settings.sla_config:
        cfg = settings.sla_config.get(sub_topic.category.code) or {}
        if "first_response_mins" in cfg:
            return cfg["first_response_mins"]
    return DEFAULT_SLA_MINUTES.get(priority)


# --------------------------------------------------------------------------- #
# Plan builders for the guardrail / fallback paths
# --------------------------------------------------------------------------- #

def _holding_plan(settings, *, status, priority, reasons, create_task=False):
    return DecisionPlan(
        action_code="holding",
        action_label="Holding reply -> agent",
        send_mode=DRAFT,
        reply_text=_holding_text(settings),
        status=status,
        priority=priority,
        reasons=reasons,
        create_agent_task=create_task,
    )


def _evidence_request_plan(sub_topic, settings, context, reasons):
    body = _template_body(sub_topic) or DEFAULT_EVIDENCE_REQUEST
    text, unresolved = templates.render(body, context)
    autosend = (
        (settings.await_evidence_autosend if settings else True)
        and _toggle(settings, Rule.ACTION_AWAIT_EVIDENCE) not in (DRAFT, "off")
        and not unresolved  # never auto-send a half-filled template
    )
    if unresolved:
        reasons = list(reasons) + ["unresolved_placeholders"]
    return DecisionPlan(
        action_code="evidence_request",
        action_label="Evidence/info request",
        send_mode=AUTO if autosend else DRAFT,
        reply_text=text,
        status=Ticket.STATUS_AWAITING_EVIDENCE if autosend else Ticket.STATUS_AWAITING_AGENT,
        priority=Ticket.PRIORITY_NORMAL,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Action -> plan mapping (the §5 table)
# --------------------------------------------------------------------------- #

def _map_action(rule, sub_topic, settings, context):
    action = rule.action
    priority = _priority_for(sub_topic, action)
    toggle = _toggle(settings, action)
    label = rule.get_action_display()

    if action == Rule.ACTION_INFO_ONLY:
        body = _template_body(sub_topic) or rule.then_response
        text, unresolved = templates.render(body, context)
        mode = toggle or "auto_send"
        if mode == "auto_send" and not unresolved:
            return DecisionPlan(
                action_code=action, action_label=label, send_mode=AUTO,
                reply_text=text, status=Ticket.STATUS_AUTO_RESOLVED,
                priority=Ticket.PRIORITY_LOW, ai_handled=True, rule_id=rule.id,
            )
        # Either toggled off auto, or the answer still has live-data placeholders.
        reasons = ["unresolved_placeholders"] if unresolved else []
        return DecisionPlan(
            action_code=action, action_label=label,
            send_mode=NONE if mode == "off" else DRAFT,
            reply_text=text, status=Ticket.STATUS_AWAITING_AGENT,
            priority=Ticket.PRIORITY_NORMAL, reasons=reasons, rule_id=rule.id,
        )

    if action == Rule.ACTION_AWAIT_EVIDENCE:
        # HARD GUARD: NEVER re-ask for evidence we already have. If the conversation already
        # carries a photo/video the claim proceeds (create) instead of looping an evidence
        # request. This is the "ticket created -> no evidence request" rule.
        if _has_evidence(context):
            logger.info("SKIP-EVIDENCE-REQUEST ticket_already_created (evidence present) -> "
                        "ACTION-CREATE-TICKET, evidence email NOT sent.")
            text, _ = templates.render(rule.then_response or "", context)
            return DecisionPlan(
                action_code=Rule.ACTION_CREATE_TICKET, action_label=label, send_mode=DRAFT,
                reply_text=text, status=Ticket.STATUS_AWAITING_AGENT, priority=priority,
                rule_id=rule.id,
            )
        logger.info("ACTION-AWAIT-EVIDENCE: no photo/video in context -> evidence request.")
        body = _template_body(sub_topic) or rule.then_response or DEFAULT_EVIDENCE_REQUEST
        text, unresolved = templates.render(body, context)
        autosend = (
            (settings.await_evidence_autosend if settings else True)
            and toggle not in (DRAFT, "off")
            and not unresolved  # never auto-send a half-filled template
        )
        return DecisionPlan(
            action_code=action, action_label=label,
            send_mode=AUTO if autosend else DRAFT, reply_text=text,
            status=(Ticket.STATUS_AWAITING_EVIDENCE if autosend
                    else Ticket.STATUS_AWAITING_AGENT),
            priority=priority, reasons=(["unresolved_placeholders"] if unresolved else []),
            rule_id=rule.id,
        )

    if action == Rule.ACTION_CREATE_TICKET:
        logger.info("ACTION-CREATE-TICKET: evidence satisfied -> create ticket, no evidence ask.")
        text, _ = templates.render(rule.then_response, context)
        return DecisionPlan(
            action_code=action, action_label=label, send_mode=DRAFT,
            reply_text=text, status=Ticket.STATUS_AWAITING_AGENT,
            priority=priority, rule_id=rule.id,
        )

    if action == Rule.ACTION_UPDATE_SYSTEM:
        return DecisionPlan(
            action_code=action, action_label=label, send_mode=NONE,
            status=Ticket.STATUS_AWAITING_AGENT, priority=priority,
            create_agent_task=True, rule_id=rule.id,
        )

    if action == Rule.ACTION_CONTINUE_CHECK:
        return DecisionPlan(
            action_code=action, action_label=label, send_mode=DRAFT,
            reply_text=_holding_text(settings), status=Ticket.STATUS_AWAITING_AGENT,
            priority=priority, reasons=["needs_live_data"], rule_id=rule.id,
        )

    if action == Rule.ACTION_TRIGGER_CRP:
        text, _ = templates.render(rule.then_response, context)
        return DecisionPlan(
            action_code=action, action_label=label, send_mode=DRAFT,
            reply_text=text, status=Ticket.STATUS_ESCALATED,
            priority=Ticket.PRIORITY_HIGH, create_agent_task=True, rule_id=rule.id,
        )

    # Unknown action -> safe agent route.
    return _holding_plan(
        settings, status=Ticket.STATUS_AWAITING_AGENT,
        priority=priority, reasons=["unknown_action"],
    )


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #

def decide(ticket, context=None):
    """Compute the DecisionPlan for a ticket without persisting anything."""
    settings = _settings_for(ticket.brand)
    ctx = dict(ticket.extracted or {})
    if context:
        ctx.update(context)

    # AUTHORITATIVE evidence presence: reflect the ticket's actual photo/video attachments
    # into ctx so NO downstream gate (requires_evidence, missing-inputs, rules) ever re-asks
    # for evidence we already have. Fixes the duplicate evidence email after ticket creation.
    if not _has_evidence(ctx):
        from apps.ingestion import evidence as _ev
        try:
            has_p, has_v = _ev.scan_attachments(
                (a.filename, a.content_type) for a in ticket.attachments.all())
            if has_p:
                ctx["has_photo"] = True
            if has_v:
                ctx["has_unboxing_video"] = True
        except Exception:  # noqa: BLE001
            pass

    sub = ticket.sub_topic_ref

    # Not classified / Uncategorized -> holding reply + agent (doc section 6 fallback).
    if sub is None:
        return _holding_plan(
            settings, status=Ticket.STATUS_AWAITING_AGENT,
            priority=Ticket.PRIORITY_NORMAL, reasons=["uncategorized"],
            create_task=True,
        )

    # Sensitive sub-topics are ALWAYS human (doc section 6).
    if sub.is_sensitive:
        return _holding_plan(
            settings, status=Ticket.STATUS_ESCALATED,
            priority=Ticket.PRIORITY_HIGH, reasons=["sensitive_subtopic"],
            create_task=True,
        )

    # Angry / escalation language -> still classified, but force agent + raise priority.
    if (ticket.sentiment or "").lower() == "angry":
        plan = _holding_plan(
            settings, status=Ticket.STATUS_ESCALATED,
            priority=Ticket.PRIORITY_HIGH, reasons=["angry_sentiment"],
        )
        return plan

    # Confidence below threshold -> draft suggestion for an agent.
    threshold = settings.confidence_threshold if settings else DEFAULT_CONFIDENCE_THRESHOLD
    if (ticket.ai_confidence or 0.0) < threshold:
        rule = sub.rules.filter(is_active=True).order_by("position").first()
        suggestion = ""
        if rule:
            suggestion, _ = templates.render(
                _template_body(sub) or rule.then_response, ctx
            )
        return DecisionPlan(
            action_code="low_confidence", action_label="Low confidence -> agent",
            send_mode=DRAFT, reply_text=suggestion or _holding_text(settings),
            status=Ticket.STATUS_AWAITING_AGENT, priority=Ticket.PRIORITY_NORMAL,
            reasons=["low_confidence"],
        )

    # AI flagged that evidence is needed -> request it (unless already provided). But
    # honor the AI hint ONLY when this case genuinely uses an evidence flow -- otherwise
    # a mis-set flag on an info category (e.g. Shipment Tracking) would bypass the
    # auto-reply policy and create a needless ticket.
    genuinely_needs_evidence = _evidence_required(ticket, sub, ctx)
    logger.info("CLASSIFICATION-TOPIC %s | CLASSIFICATION-SUBTOPIC %s | REQUIRES-EVIDENCE "
                "ai_hint=%s genuinely_required=%s", ticket.category or "-",
                ticket.sub_topic or "-", bool(ctx.get("requires_evidence")),
                genuinely_needs_evidence)
    if ctx.get("requires_evidence") and genuinely_needs_evidence:
        # HARD GUARD (BUG fix): never request evidence if it is ALREADY present -- either via
        # the context flags OR an actual photo/video attachment on the ticket. The ticket is
        # only created once evidence is received, so a created ticket with an attachment must
        # NEVER trigger a second evidence-request email.
        if _has_evidence(ctx) or _ticket_has_evidence_attachment(ticket):
            logger.info("SKIP-EVIDENCE-REQUEST ticket_already_created (evidence present: "
                        "ctx=%s attachment=%s) -> evidence email NOT sent.",
                        _has_evidence(ctx), _ticket_has_evidence_attachment(ticket))
        else:
            logger.info("DECISION-ACTION evidence_request | EVIDENCE-REQUEST-REASON "
                        "requires_evidence (sub=%s)", ticket.sub_topic or "-")
            return _evidence_request_plan(sub, settings, ctx, reasons=["requires_evidence"])

    # AI flagged that a human must handle this -> draft + route to agent.
    if ctx.get("requires_agent"):
        rule = sub.rules.filter(is_active=True).order_by("position").first()
        suggestion = ""
        if rule:
            suggestion, _ = templates.render(_template_body(sub) or rule.then_response, ctx)
        return DecisionPlan(
            action_code="requires_agent", action_label="AI flagged -> agent",
            send_mode=DRAFT, reply_text=suggestion or _holding_text(settings),
            status=Ticket.STATUS_AWAITING_AGENT, priority=Ticket.PRIORITY_NORMAL,
            reasons=["requires_agent"],
        )

    # Missing mandatory inputs -> ask instead of guessing (doc section 6). BUT never send
    # the evidence-request template once evidence is present (the ticket is already created):
    # the agent chases the missing field instead of bouncing the customer with a re-ask.
    missing = [k for k in (sub.mandatory_inputs or []) if not ctx.get(k)]
    if missing:
        if _has_evidence(ctx) or _ticket_has_evidence_attachment(ticket):
            logger.info("SKIP-EVIDENCE-REQUEST ticket_already_created (evidence present) -> "
                        "not re-asking despite missing inputs %s.", missing)
        else:
            return _evidence_request_plan(
                sub, settings, ctx, reasons=[f"missing_inputs:{','.join(missing)}"]
            )

    # Run the IF/THEN/Action rules.
    rule, failure = select_rule(sub, ctx)
    if rule is None:
        reasons = [failure or "no_matching_rule"]
        plan = _holding_plan(
            settings, status=Ticket.STATUS_AWAITING_AGENT,
            priority=_priority_for(sub, Rule.ACTION_CREATE_TICKET), reasons=reasons,
        )
    else:
        plan = _map_action(rule, sub, settings, ctx)

    # Apply the per-category business rules (money / account / fraud never auto-reply).
    plan = _constrain_to_category(plan, sub.category.code)
    # Final guardrail: the Auto-Reply vs Ticket spec decides ticket vs auto-reply&close.
    return _apply_ticket_policy(plan, ticket, sub, ctx, settings)


def _ticket_policy_text(ticket, sub, ctx):
    return " ".join(filter(None, [
        sub.name if sub else "", ticket.subject or "",
        ticket.issue_summary or "", str(ctx.get("issue_summary") or ""),
        str(ctx.get("customer_intent") or ""),
    ]))


# A reason that means the engine genuinely needs live data a human must fetch -> never
# force an auto-reply over it. (An unresolved TEMPLATE placeholder is handled differently:
# for an info category we blank the half-filled text and let the responder rewrite it,
# rather than drafting it into a ticket.)
_NO_AUTOREPLY_REASONS = {"needs_live_data"}


def _apply_ticket_policy(plan, ticket, sub, ctx, settings):
    """Decide ticket vs auto-reply per policy.requires_ticket (the customer's spec).

    Runs ONLY on the confident, normal path -- sensitive / low-confidence / evidence /
    requires_agent / angry already returned a human route earlier in decide(), so they
    keep going to an agent (and a ticket), which is correct.

    - TICKET category  -> never auto-resolve: force a human-actioned ticket (so the
      Care Panel ticket + tracking link are created and sent).
    - NO_TICKET category -> auto-reply & close (Route A): no ticket, no tracking link.
      But the brand's automation toggle (off / draft), live-data needs, and unresolved
      placeholders are respected -- those legitimately keep a human in the loop."""
    from . import policy

    if sub is None:
        return plan
    text = _ticket_policy_text(ticket, sub, ctx)
    if policy.requires_ticket(sub.category.code, sub.name, text):
        # ACTION category -> must create a ticket: never auto-resolve & close.
        if plan.ai_handled or plan.status == Ticket.STATUS_AUTO_RESOLVED:
            plan.status = Ticket.STATUS_AWAITING_AGENT
            plan.ai_handled = False
            plan.send_mode = DRAFT if plan.reply_text else NONE
            plan.reasons = list(plan.reasons) + ["policy_requires_ticket"]
        return plan

    # NO_TICKET (info / self-serve) -> auto-reply & close, when it's safe to do so.
    if plan.status == Ticket.STATUS_AUTO_RESOLVED:
        return plan                                   # already auto-answering
    # Only an INFO answer or a generic no-rule holding may be auto-closed. A deliberate
    # create_ticket / await_evidence / escalation (e.g. a delayed order in cat 1) is a
    # real action the engine chose -> keep it a ticket.
    if plan.action_code not in (Rule.ACTION_INFO_ONLY, "holding"):
        return plan
    if _NO_AUTOREPLY_REASONS & set(plan.reasons or []):
        return plan                                   # needs live data / half-filled
    # Respect an EXPLICIT brand toggle (off/draft) for this action. A coarse category
    # downgrade (e.g. the old cat-14 "agent" rule) is intentionally overridden here --
    # the finer sub-topic policy is authoritative for the auto-reply decision.
    if _toggle(settings, plan.action_code) in (DRAFT, "off"):
        return plan

    # Safe to auto-answer & close. A holding (no-rule) plan carries only generic text,
    # and a half-filled template (unresolved live-data placeholder, e.g. {tracking_url}
    # the lookup couldn't fill) must NOT be sent verbatim -- blank either so the AI
    # responder writes a real answer (the safety-net downgrades to an agent if it can't).
    unresolved = "unresolved_placeholders" in (plan.reasons or [])
    if plan.action_code == "holding" or unresolved:
        plan.reply_text = ""
    plan.send_mode = AUTO
    plan.status = Ticket.STATUS_AUTO_RESOLVED
    plan.ai_handled = True
    plan.priority = Ticket.PRIORITY_LOW
    plan.create_agent_task = False
    plan.reasons = [r for r in plan.reasons if r != "unresolved_placeholders"] \
        + ["policy_auto_reply"]
    return plan


def _constrain_to_category(plan, category_code):
    """Downgrade a plan to honor the category business rules (policy.py)."""
    from . import policy

    pol = policy.policy_for(category_code)
    if pol in (None, policy.POLICY_AUTO_REPLY):
        return plan  # auto-reply permitted; the engine's decision stands

    if pol == policy.POLICY_ESCALATE:
        if plan.send_mode == AUTO:
            plan.send_mode = DRAFT
        plan.status = Ticket.STATUS_ESCALATED
        plan.priority = Ticket.PRIORITY_HIGH
        plan.create_agent_task = True
        plan.ai_handled = False
        plan.reasons = list(plan.reasons) + ["category_escalate"]
    elif pol in (policy.POLICY_DRAFT_AGENT, policy.POLICY_AGENT):
        # Only downgrade if the engine wanted to auto-send; leave already-agent
        # plans (drafts / escalations) as they are.
        if plan.send_mode == AUTO:
            plan.send_mode = DRAFT if pol == policy.POLICY_DRAFT_AGENT else NONE
            plan.status = Ticket.STATUS_AWAITING_AGENT
            plan.ai_handled = False
            plan.reasons = list(plan.reasons) + [f"category_{pol}"]
    return plan


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def apply_decision(ticket, plan, actor="ai"):
    """Persist a DecisionPlan: outbound message (sent/draft), status, priority,
    SLA, audit. Returns the created outbound Message (or None)."""
    # Auto-reply ONLY after a successful Gemini classification (spec rule 7).
    # Anything classified by the rule fallback / not yet AI-classified is drafted
    # for an agent instead of auto-sent.
    if plan.send_mode == AUTO and ticket.classification_status != Ticket.CLS_CLASSIFIED:
        plan.send_mode = DRAFT
        plan.ai_handled = False
        if plan.status == Ticket.STATUS_AUTO_RESOLVED:
            plan.status = Ticket.STATUS_AWAITING_AGENT
        plan.reasons = list(plan.reasons) + ["ai_not_classified"]

    # If the plan wants to send/draft a reply but has no template text, ask the AI
    # to generate an appropriate response (spec step 8). Safe no-op without a key.
    if not plan.reply_text and plan.send_mode in (AUTO, DRAFT):
        try:
            from apps.classifier.responder import generate_reply

            generated = generate_reply(ticket)
            if generated:
                plan.reply_text = generated
        except Exception:  # noqa: BLE001
            logger.exception("Reply generation hook failed for %s", ticket.ticket_id)

    # Safety: a NO_TICKET auto-reply we couldn't actually answer must NOT silently close
    # with no message and no ticket -- downgrade to an agent so a human responds (and a
    # ticket is created). Guards against an info category with no template and no AI key.
    if plan.status == Ticket.STATUS_AUTO_RESOLVED and not plan.reply_text:
        plan.status = Ticket.STATUS_AWAITING_AGENT
        plan.ai_handled = False
        plan.reasons = list(plan.reasons) + ["no_auto_answer"]

    # Action router (§4): the route (A/B/C/D) drives the dashboard AND, for Route A,
    # the M4 "auto-answer & close" wrapper (so the reply tells the customer it closes
    # the request / reply to reopen).
    route, route_label = route_for(ticket, plan)

    message = None
    if plan.reply_text and plan.send_mode in (AUTO, DRAFT):
        is_draft = plan.send_mode == DRAFT
        if route == "A" and plan.send_mode == AUTO:
            from apps.ingestion import mails

            m4_subject, body = mails.render("M4", ticket.language, answer=plan.reply_text)
            subject = f"Re: {ticket.subject}" if ticket.subject else m4_subject
        else:
            subject, body = f"Re: {ticket.subject}", plan.reply_text
        message = Message.objects.create(
            ticket=ticket,
            direction=Message.DIRECTION_OUTBOUND,
            from_email=ticket.mailbox.email_address if ticket.mailbox else "",
            to_email=ticket.customer_email,
            subject=subject,
            body_text=body,
            is_draft=is_draft,
            sent_at=None if is_draft else timezone.now(),
        )
        if plan.send_mode == AUTO:
            try:
                from apps.ingestion import service as ingestion

                ingestion.send_reply(message)
            except Exception:  # noqa: BLE001 -- delivery is best-effort
                logger.exception("Auto-send failed for ticket %s", ticket.ticket_id)

    ticket.status = plan.status
    ticket.priority = plan.priority
    ticket.action_taken = plan.action_label
    if plan.ai_handled:
        ticket.ai_handled = True

    ticket.extracted = {**(ticket.extracted or {}), "route": route,
                        "route_label": route_label}

    minutes = (
        None if plan.status == Ticket.STATUS_AUTO_RESOLVED
        else _sla_minutes(_settings_for(ticket.brand), ticket.sub_topic_ref, plan.priority)
        if ticket.sub_topic_ref else DEFAULT_SLA_MINUTES.get(plan.priority)
    )
    ticket.sla_due_at = (
        timezone.now() + timezone.timedelta(minutes=minutes) if minutes else None
    )
    ticket.save()

    AuditLogEntry.objects.create(
        ticket=ticket, actor=actor, event="decision",
        detail={
            "action": plan.action_code,
            "route": route,
            "route_label": route_label,
            "send_mode": plan.send_mode,
            "status": plan.status,
            "priority": plan.priority,
            "auto_sent": plan.send_mode == AUTO,
            "reasons": plan.reasons,
            "rule_id": plan.rule_id,
        },
    )
    if plan.create_agent_task:
        AuditLogEntry.objects.create(
            ticket=ticket, actor=actor, event="agent_task",
            detail={"action": plan.action_code, "label": plan.action_label},
        )
    return message


def run(ticket, context=None, actor="ai"):
    """decide + apply in one call. No-op (returns None) on ignored tickets."""
    if ticket.is_ignored:
        return None
    plan = decide(ticket, context=context)
    apply_decision(ticket, plan, actor=actor)
    return plan
