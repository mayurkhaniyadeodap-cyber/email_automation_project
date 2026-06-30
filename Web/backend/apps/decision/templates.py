"""
Reply-text rendering for the decision engine (doc section 5 / Templates in §10).

Templates support placeholders like {order_id}, {tracking_url}, {edd}. We render
only the placeholders we actually have values for and REPORT the ones we don't,
so the engine can refuse to auto-send a half-filled answer (a placeholder that
needs live Shopify/Shipping data stays literal -> the engine drafts instead).
"""

import re

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render(text, context):
    """Return (rendered_text, unresolved_keys) for a template body.

    `context` is a flat dict (ticket.extracted merged with any live data). Keys
    present (and not None/"") are substituted; everything else is left as a literal
    {placeholder} and listed in unresolved_keys.
    """
    if not text:
        return "", []

    unresolved = []

    def _sub(match):
        key = match.group(1)
        value = context.get(key)
        if value in (None, ""):
            unresolved.append(key)
            return match.group(0)  # leave the literal {key}
        return str(value)

    rendered = _PLACEHOLDER_RE.sub(_sub, text)
    # De-dup while preserving order.
    seen = set()
    unresolved = [k for k in unresolved if not (k in seen or seen.add(k))]
    return rendered, unresolved
