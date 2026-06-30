"""
Thin Gmail API wrapper (doc section 2).

One Google project, one OAuth credential, one watch per brand mailbox. Per-mailbox
OAuth tokens are stored on `Mailbox.oauth_payload`. Everything google-specific is
imported lazily inside methods so the rest of the engine -- and the offline test
suite -- runs without the google-api-python-client / google-auth packages.

Required scope: https://www.googleapis.com/auth/gmail.modify  (read + send + label).

This is intentionally a small surface the ingestion service calls through:
    list_history / list_recent_message_ids / get_message / latest_history_id
    send_message / start_watch
Tests inject a fake object exposing the same methods, so none of this needs a
live Google connection to exercise the ingestion flow.
"""

import base64
import logging
from email.mime.text import MIMEText

from django.conf import settings

logger = logging.getLogger(__name__)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


class GmailClientError(RuntimeError):
    pass


class GmailClient:
    def __init__(self, mailbox, service):
        self.mailbox = mailbox
        self.user_id = mailbox.email_address
        self._service = service

    # -- construction -----------------------------------------------------

    @classmethod
    def for_mailbox(cls, mailbox):
        """Build an authenticated client from the mailbox's stored OAuth payload.

        Returns None (rather than raising) when the mailbox has no credentials
        yet, so callers can degrade gracefully to "record locally only".
        """
        payload = mailbox.oauth_payload or {}
        if not payload:
            logger.warning("Mailbox %s has no OAuth payload; Gmail disabled.", mailbox.email_address)
            return None
        try:
            service = cls._build_service(payload)
        except Exception as exc:  # noqa: BLE001 -- surface any google/auth failure as disabled
            logger.exception("Failed to build Gmail service for %s: %s", mailbox.email_address, exc)
            return None
        return cls(mailbox, service)

    @staticmethod
    def _build_service(payload):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=payload.get("token"),
            refresh_token=payload.get("refresh_token"),
            token_uri=payload.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=payload.get("client_id") or settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=payload.get("client_secret") or settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=[GMAIL_SCOPE],
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # -- ingestion reads --------------------------------------------------

    def list_history(self, start_history_id):
        """Message ids added since start_history_id (doc section 2, users.history.list)."""
        message_ids = []
        page_token = None
        while True:
            resp = (
                self._service.users()
                .history()
                .list(
                    userId=self.user_id,
                    startHistoryId=str(start_history_id),
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
            for h in resp.get("history", []):
                for added in h.get("messagesAdded", []):
                    mid = added.get("message", {}).get("id")
                    if mid:
                        message_ids.append(mid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        # Preserve order, drop dups.
        seen = set()
        return [m for m in message_ids if not (m in seen or seen.add(m))]

    def list_recent_message_ids(self, max_results=25):
        """Most recent inbox message ids -- used to seed the first sync."""
        resp = (
            self._service.users()
            .messages()
            .list(userId=self.user_id, labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]

    def get_message(self, message_id):
        """Full message resource (doc section 2, users.messages.get format=full)."""
        return (
            self._service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )

    def latest_history_id(self):
        """The mailbox's current historyId (from the profile)."""
        profile = self._service.users().getProfile(userId=self.user_id).execute()
        return profile.get("historyId")

    def get_attachment(self, message_id, attachment_id):
        """Fetch an attachment's data (base64url) for evidence handling (doc §13)."""
        return (
            self._service.users()
            .messages()
            .attachments()
            .get(userId=self.user_id, messageId=message_id, id=attachment_id)
            .execute()
            .get("data", "")
        )

    # -- sending ----------------------------------------------------------

    def send_message(self, *, thread_id, to, subject, body_text, in_reply_to="", references=None):
        """Send a reply in-thread (doc section 2, users.messages.send)."""
        mime = MIMEText(body_text or "", _charset="utf-8")
        mime["To"] = to
        mime["From"] = self.user_id
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
        if references:
            mime["References"] = " ".join(references)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        sent = (
            self._service.users()
            .messages()
            .send(userId=self.user_id, body=body)
            .execute()
        )
        return sent.get("id")

    # -- watch ------------------------------------------------------------

    def start_watch(self, topic_name):
        """(Re)register a Pub/Sub watch (doc section 2, users.watch). Returns the
        {historyId, expiration} response so the mailbox can store the baseline."""
        resp = (
            self._service.users()
            .watch(
                userId=self.user_id,
                body={"topicName": topic_name, "labelIds": ["INBOX"]},
            )
            .execute()
        )
        return resp
