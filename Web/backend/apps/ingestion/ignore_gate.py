"""
The Ignore / Block gate (doc section 3) -- the first filter, before a mail becomes
a real ticket. Nothing here is hardcoded: every rule is a `BlockListEntry` row in
the brand's Settings, so support staff can add/edit/delete them per brand.

A mail is IGNORED (logged, flagged, shown in the separate "Ignored" tab -- never
in the main queue, never auto-replied) if ANY active block-list entry matches.

The six rule kinds map to BlockListEntry.KIND_* :
    sender_email   exact sender address                     buyer@spammer.com
    sender_domain  sender domain (supports *@domain / domain) *@newsletter.xyz
    noreply        local-part / address token               noreply@, mailer-daemon@
    internal       your own staff/system domain or address  @deodap.com
    marketing      a header token to detect bulk mail       List-Unsubscribe
                                                            Precedence: bulk
    spam           a token to find in Authentication-Results / subject
                                                            dmarc=fail
"""

from dataclasses import dataclass

from apps.brand_settings.models import BlockListEntry


@dataclass
class IgnoreResult:
    ignored: bool
    kind: str = ""
    reason: str = ""

    def __bool__(self):
        return self.ignored


_NOT_IGNORED = IgnoreResult(False)


def _domain_of(email):
    return email.split("@")[-1] if "@" in email else ""


def _matches_domain(domain, pattern):
    """Match a sender domain against a block pattern like '*@newsletter.xyz' or
    'newsletter.xyz' (also matches sub-domains)."""
    p = pattern.lower().strip()
    p = p.lstrip("*").lstrip("@")  # '*@newsletter.xyz' -> 'newsletter.xyz'
    if not p:
        return False
    return domain == p or domain.endswith("." + p)


def _header_token_matches(headers, token):
    """A marketing/spam token is either 'Header-Name' (presence) or
    'Header-Name: substring' (value contains substring), case-insensitive."""
    lower_headers = {k.lower(): (v or "") for k, v in headers.items()}
    token = token.strip()
    if ":" in token:
        name, _, expected = token.partition(":")
        name = name.strip().lower()
        expected = expected.strip().lower()
        value = lower_headers.get(name)
        if value is None:
            return False
        return expected in value.lower() if expected else True
    # Bare token: treat as "header present" OR "token appears in any header value".
    name = token.lower()
    if name in lower_headers:
        return True
    return any(name in v.lower() for v in lower_headers.values())


def _entry_matches(entry, *, from_email, domain, local_part, headers):
    kind, value = entry.kind, (entry.value or "").strip()
    if not value:
        return False
    v = value.lower()

    if kind == BlockListEntry.KIND_SENDER:
        return from_email == v

    if kind == BlockListEntry.KIND_DOMAIN:
        return _matches_domain(domain, value)

    if kind == BlockListEntry.KIND_NOREPLY:
        # 'noreply@' / 'mailer-daemon@' -> match the local part or whole address.
        token = v.rstrip("@")
        return token and (token in local_part or v in from_email)

    if kind == BlockListEntry.KIND_INTERNAL:
        # '@deodap.com' / 'deodap.com' -> domain match; exact address -> exact match.
        if v.startswith("@") or "@" not in v:
            return _matches_domain(domain, value)
        return from_email == v

    if kind in (BlockListEntry.KIND_MARKETING, BlockListEntry.KIND_SPAM):
        return _header_token_matches(headers, value)

    return False


def evaluate(brand, message):
    """Run the ignore gate for a normalized message against the brand's block list.

    Returns an IgnoreResult (truthy when the mail should be ignored). The first
    matching entry wins, so the stored reason points at exactly one rule.
    """
    from_email = (message.get("from_email") or "").lower().strip()
    domain = _domain_of(from_email)
    local_part = from_email.split("@")[0] if "@" in from_email else from_email
    headers = message.get("headers") or {}

    entries = brand.block_list.filter(is_active=True)
    for entry in entries:
        if _entry_matches(
            entry,
            from_email=from_email,
            domain=domain,
            local_part=local_part,
            headers=headers,
        ):
            return IgnoreResult(
                ignored=True,
                kind=entry.kind,
                reason=f"{entry.get_kind_display()}: {entry.value}",
            )
    return _NOT_IGNORED
