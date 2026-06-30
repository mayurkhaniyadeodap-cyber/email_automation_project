"""
The classifier "Skills" knowledge base (doc sections 4 & 5).

The prompt is built FROM the brand's own taxonomy: each sub-topic's question plus
its IF/THEN/Action rules become the knowledge the model must match the mail
against. The AI never invents a category -- it must map to one of these fixed
codes, or to the `Uncategorized` fallback (which routes to an agent).

Everything here is per-brand, so editing categories/rules in Settings instantly
changes what the classifier knows (doc sections 9 & 10).
"""

from apps.taxonomy.models import Category

# The fixed fallback when nothing in the taxonomy fits (doc section 4).
UNCATEGORIZED = "Uncategorized"


def build_knowledge_base(brand):
    """Render the brand's taxonomy into the compact reference block for the prompt."""
    lines = []
    categories = (
        Category.objects.filter(brand=brand, is_active=True)
        .prefetch_related("sub_topics__rules")
        .order_by("position", "code")
    )
    for cat in categories:
        lines.append(f"{cat.code}. {cat.name}")
        for sub in cat.sub_topics.filter(is_active=True).order_by("position", "code"):
            flag = " [SENSITIVE -> always human]" if sub.is_sensitive else ""
            lines.append(f"  {sub.code} {sub.name}{flag}")
            if sub.question:
                lines.append(f"      Q: {sub.question}")
            if sub.mandatory_inputs:
                lines.append(f"      needs: {', '.join(sub.mandatory_inputs)}")
            for rule in sub.rules.filter(is_active=True).order_by("position"):
                cond = rule.condition or "(default)"
                lines.append(f"      IF {cond} -> [{rule.get_action_display()}]")
    return "\n".join(lines)


SYSTEM_PROMPT = """\
You are a customer-support email classifier for the brand "{brand}".

Map the customer's email to EXACTLY ONE category and ONE sub-topic from the FIXED
taxonomy below. You must NOT invent new categories or sub-topics. If the mail does
not fit any sub-topic, set "category" to "{uncategorized}", "sub_topic" to "", and
"confidence" to 0.4 or lower.

Use the leading codes exactly as written (e.g. category "3. Delivery Issues
(Post-Delivery)" and sub_topic "3.3 Shipment Lost or Damaged").

FIXED TAXONOMY (category -> sub-topics -> their decision rules):
{knowledge_base}

Not every email is a customer support request. Set "is_support_request" to false ONLY for
marketing / promotional emails, newsletters, OTP / verification codes, internal reports,
system notifications, no-reply / automated mail, and spam -- these must NOT become tickets.
A genuine inquiry from a person -- including franchise / dealer / reseller / dropship /
wholesale / bulk / "become a seller" inquiries (category 11) and product / store / company
questions -- IS a support request: set "is_support_request" true and assign the category.

Return STRICT JSON ONLY (no markdown, no prose) with this exact shape:
{{
  "is_support_request": <true|false>,
  "category": "<one category line from the taxonomy, or '{uncategorized}'>",
  "sub_topic": "<one sub-topic line, or ''>",
  "confidence": <float 0..1>,
  "issue_summary": "<one-sentence summary of the customer's problem>",
  "requires_evidence": <true if you need photos/video to resolve, else false>,
  "requires_agent": <true if a human agent must handle this, else false>,
  "action": "<auto_reply | request_evidence | assign_agent | create_draft>",
  "extracted": {{
    "order_id": <string or null>,
    "phone": <customer phone number (digits only) or null>,
    "awb": <string or null>,
    "has_unboxing_video": <true|false>,
    "has_photo": <true|false>,
    "customer_intent": "<short phrase>"
  }},
  "language": "<ISO code, e.g. en, hi>",
  "sentiment": "<neutral|happy|frustrated|angry>"
}}
"""


def build_prompt(brand, message):
    """Return (system_prompt, user_prompt) for a normalized message dict.

    `message` only needs from_email / subject / body_text / attachments keys, so a
    Ticket's latest inbound mail can be passed just as easily as a fresh ingest.
    """
    system = SYSTEM_PROMPT.format(
        brand=brand.name,
        uncategorized=UNCATEGORIZED,
        knowledge_base=build_knowledge_base(brand),
    )

    attachments = message.get("attachments") or []
    att_summary = (
        ", ".join(
            f"{a.get('filename', '?')} ({a.get('mime_type', '?')})" for a in attachments
        )
        or "none"
    )
    body = (message.get("body_text") or "").strip()
    if not body:
        # Fall back to a snippet of HTML-stripped content if only HTML arrived.
        body = (message.get("snippet") or "").strip()

    user = (
        f"From: {message.get('from_email', '')}\n"
        f"Subject: {message.get('subject', '')}\n"
        f"Attachments: {att_summary}\n\n"
        f"Body:\n{body}"
    )
    return system, user
