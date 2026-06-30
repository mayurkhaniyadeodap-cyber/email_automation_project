"""
Generate an appropriate customer-reply with the brand's AI provider (spec step 8).

Used by the decision engine to draft/auto-send a reply for categories that allow it.
Best-effort: returns None when no provider is configured or generation fails, so the
engine falls back to templates / holding text.
"""

import logging

from . import service

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are a customer-support agent for the brand \"{brand}\". Write a concise, "
    "polite reply (plain text, 2-5 sentences) to the customer's email below. "
    "Category: {category} / {sub_topic}. Be helpful and specific to their issue, but "
    "NEVER invent order details, dates, tracking numbers or amounts you weren't given. "
    "If you lack the info to fully resolve it, acknowledge the request and say the team "
    "is on it. Do not include a subject line; reply body only."
)


def generate_reply(ticket, provider=None):
    """Return a generated reply string for a ticket, or None."""
    settings = service._settings_for(ticket.brand)
    provider = provider or service.build_provider(settings)
    if provider is None:
        return None

    msg = service._message_dict_from_ticket(ticket)
    system = SYSTEM.format(
        brand=ticket.brand.name,
        category=ticket.category or "Uncategorized",
        sub_topic=ticket.sub_topic or "",
    )
    user = (
        f"From: {msg.get('from_email', '')}\n"
        f"Subject: {msg.get('subject', '')}\n\n"
        f"{msg.get('body_text', '')}"
    )
    try:
        text = provider.generate_text(system, user)
        return (text or "").strip() or None
    except Exception:  # noqa: BLE001 -- best-effort; fall back to templates
        logger.exception("Reply generation failed for ticket %s", ticket.ticket_id)
        return None
