"""
Waiting-state timers (DeoDap Care — Final Mail Flow v2.0, §8).

A pending conversation that is waiting on the customer (for evidence, a video, an
order id or a phone) gets:

    * a single reminder mail (M7R) after settings.REMINDER_HOURS  (default 24h),
    * an auto-close mail (M7C) after settings.AUTOCLOSE_HOURS      (default 72h),
      moving it to status "closed".

A customer reply within settings.REOPEN_DAYS (default 7) reopens the same case
(handled in service._find_pending / the pending-reply gate); after that window a
reply starts fresh.

`sweep_waiting_states()` is one idempotent tick, run by the scheduler. It is safe
to call repeatedly: reminder_sent_at guards the reminder, status="closed" guards
the close.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.tickets.models import PendingConversation

logger = logging.getLogger(__name__)

# States that are genuinely waiting on the customer.
WAITING_STATUSES = ("awaiting_evidence", "waiting_for_video")


def _waiting_for(pending):
    """Human phrase describing what we're still waiting on (for the M7R reminder)."""
    from . import evidence, service

    level = service._pending_evidence_level(pending)
    if level == evidence.EV_VIDEO and not pending.has_video:
        return "a clear video of the issue"
    if level == evidence.EV_PHOTO and not pending.has_evidence:
        return "a clear photo of the issue"
    if not service._has_identifier(pending):
        return "your order number, email or registered mobile number"
    return "the required details"


def _send_pending_mail(pending, mail_id, **vars):
    """Render an M-series mail and send it on the pending's thread. Returns sent id."""
    from . import mails, service

    subject, body = mails.render(mail_id, pending.language, **vars)
    if pending.subject:
        subject = f"Re: {pending.subject}"
    refs = list(pending.references or [])
    if pending.original_message_id and pending.original_message_id not in refs:
        refs.append(pending.original_message_id)
    return service._send_customer_email(
        pending.customer_email, subject, body,
        in_reply_to=pending.original_message_id, references=refs,
    )


def sweep_waiting_states(now=None):
    """One sweep: send 24h reminders and 72h auto-closes. Returns (reminded, closed)."""
    now = now or timezone.now()
    reminder_after = timedelta(hours=int(getattr(settings, "REMINDER_HOURS", 24)))
    close_after = timedelta(hours=int(getattr(settings, "AUTOCLOSE_HOURS", 72)))

    reminded = closed = 0
    for pending in PendingConversation.objects.filter(status__in=WAITING_STATUSES):
        age = now - pending.created_at
        if age >= close_after:
            _send_pending_mail(pending, "M7C")
            pending.status = "closed"
            pending.closed_at = now
            pending.save(update_fields=["status", "closed_at", "updated_at"])
            logger.info("WAITING-AUTOCLOSE pending=%s age_h=%.1f",
                        pending.id, age.total_seconds() / 3600)
            closed += 1
        elif pending.reminder_sent_at is None and age >= reminder_after:
            sent_id = _send_pending_mail(pending, "M7R", missing=_waiting_for(pending))
            pending.reminder_sent_at = now
            if sent_id:
                pending.last_message_id = sent_id
            pending.save(update_fields=["reminder_sent_at", "last_message_id", "updated_at"])
            logger.info("WAITING-REMINDER pending=%s age_h=%.1f",
                        pending.id, age.total_seconds() / 3600)
            reminded += 1

    if reminded or closed:
        logger.info("WAITING-SWEEP reminded=%d closed=%d", reminded, closed)
    return reminded, closed
