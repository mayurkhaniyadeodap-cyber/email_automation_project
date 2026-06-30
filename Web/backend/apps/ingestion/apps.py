import os
import sys

from django.apps import AppConfig


class IngestionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ingestion"
    label = "ingestion"

    def ready(self):
        # Auto-fetch only under the running server -- not during migrate/test/shell.
        argv = " ".join(sys.argv)
        if "runserver" not in argv or "test" in sys.argv:
            return
        # Django's autoreloader runs ready() in BOTH the watcher and the worker process.
        # Start the scheduler ONLY in the worker (RUN_MAIN=true) -- otherwise two
        # schedulers run per server and every email is fetched + answered twice.
        if os.environ.get("RUN_MAIN") != "true":
            return
        try:
            from apps.ingestion import scheduler

            scheduler.start()
        except Exception:  # noqa: BLE001 -- a scheduler hiccup must not block startup
            import logging
            logging.getLogger(__name__).exception("Could not start auto-fetch scheduler")
