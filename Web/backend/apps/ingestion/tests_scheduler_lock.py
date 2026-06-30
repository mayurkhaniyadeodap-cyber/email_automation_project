"""
Regression: only ONE scheduler may run, so the inbox is never fetched + answered
twice (the reported duplicate-reply bug from multiple runserver instances).

    python manage.py test apps.ingestion.tests_scheduler_lock
"""

import socket

from django.test import TestCase, override_settings

from apps.ingestion import scheduler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SchedulerLockTests(TestCase):
    def tearDown(self):
        if scheduler._lock_socket is not None:
            scheduler._lock_socket.close()
            scheduler._lock_socket = None

    def test_only_one_instance_wins_the_lock(self):
        port = _free_port()
        with override_settings(SCHEDULER_LOCK_PORT=port):
            self.assertTrue(scheduler._acquire_single_instance_lock())   # 1st wins
            won = scheduler._lock_socket
            self.assertFalse(scheduler._acquire_single_instance_lock())  # 2nd blocked
            # the original lock socket is untouched (still held by the winner)
            self.assertIs(scheduler._lock_socket, won)

    def test_lock_frees_when_socket_closed(self):
        port = _free_port()
        with override_settings(SCHEDULER_LOCK_PORT=port):
            self.assertTrue(scheduler._acquire_single_instance_lock())
            scheduler._lock_socket.close()
            scheduler._lock_socket = None
            # released -> a fresh acquire succeeds again
            self.assertTrue(scheduler._acquire_single_instance_lock())
