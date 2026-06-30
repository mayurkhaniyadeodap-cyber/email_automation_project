"""
In-process auto-fetch scheduler. Runs the IMAP/Gmail fetch every
settings.AUTO_FETCH_MINUTES minutes while the dev server is up -- no Celery/Redis.

Started from IngestionConfig.ready(); guarded so it only runs under `runserver`
(not during migrations, tests, or other management commands).
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)
_scheduler = None
_lock_socket = None


def _lock_port():
    # Single-instance lock port (read at call time so tests can override it).
    return int(getattr(settings, "SCHEDULER_LOCK_PORT", 0) or 47321)


def _acquire_single_instance_lock():
    """True if THIS process won the scheduler lock. The first scheduler binds a
    localhost port; any other process (e.g. a second `runserver`) fails to bind and
    does NOT start a scheduler, so the inbox is never fetched/answered twice. The
    socket auto-releases when the process exits."""
    global _lock_socket
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _lock_port()))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_socket = s  # keep a reference so the bind persists for the process lifetime
    return True


def fetch_all_mailboxes():
    """Fetch new mail for every active mailbox (one scheduler tick)."""
    from apps.ingestion import service
    from apps.organizations.models import Mailbox

    provider = getattr(settings, "EMAIL_PROVIDER", "imap")
    total = 0
    for mailbox in Mailbox.objects.filter(is_active=True):
        try:
            if provider == "imap":
                results = service.fetch_imap(mailbox)
            else:
                if service.build_client(mailbox) is None:
                    continue
                results = service.sync_history(mailbox)
            new = sum(1 for r in results if len(r) > 2 and r[2])
            total += new
        except Exception:  # noqa: BLE001 -- never let one mailbox kill the loop
            logger.exception("Auto-fetch failed for %s", mailbox.email_address)
    if total:
        logger.info("Auto-fetch: %d new email(s) ingested.", total)
    return total


def sweep_waiting_states():
    """Scheduler tick for the waiting-state timers (24h reminder / 72h auto-close)."""
    from apps.ingestion import timers

    try:
        return timers.sweep_waiting_states()
    except Exception:  # noqa: BLE001 -- never let the sweep kill the scheduler
        logger.exception("Waiting-state sweep failed")
        return (0, 0)


def start():
    """Start the background scheduler (idempotent): auto-fetch mail + waiting-state
    timers. Auto-fetch runs when AUTO_FETCH_MINUTES > 0; the sweep when
    WAITING_SWEEP_MINUTES > 0."""
    global _scheduler
    fetch_minutes = int(getattr(settings, "AUTO_FETCH_MINUTES", 0) or 0)
    sweep_minutes = int(getattr(settings, "WAITING_SWEEP_MINUTES", 0) or 0)
    if (fetch_minutes <= 0 and sweep_minutes <= 0) or _scheduler is not None:
        return

    # Only ONE scheduler may run across all processes -- otherwise each extra server
    # re-fetches the same mail and the customer gets duplicate replies.
    if not _acquire_single_instance_lock():
        logger.warning("Auto-fetch scheduler NOT started: another instance already "
                       "holds the lock (port %d). Avoiding duplicate fetches.",
                       _lock_port())
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone=str(getattr(settings, "TIME_ZONE", "UTC")))
    if fetch_minutes > 0:
        _scheduler.add_job(
            fetch_all_mailboxes, "interval", minutes=fetch_minutes,
            id="auto_fetch_mail", replace_existing=True, max_instances=1, coalesce=True,
        )
    if sweep_minutes > 0:
        _scheduler.add_job(
            sweep_waiting_states, "interval", minutes=sweep_minutes,
            id="waiting_state_sweep", replace_existing=True, max_instances=1, coalesce=True,
        )
    _scheduler.start()
    logger.info("Scheduler started: auto-fetch every %d min, waiting-sweep every %d min.",
                fetch_minutes, sweep_minutes)
