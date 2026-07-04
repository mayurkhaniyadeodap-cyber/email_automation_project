"""Backfill Ticket.last_activity_at for existing rows so the Gmail-style Inbox (ordered by
latest message activity) sorts correctly from day one. Value = the newest non-draft message's
timestamp, falling back to updated_at / created_at. agent_unread stays False for history."""

from django.db import migrations


def backfill(apps, schema_editor):
    Ticket = apps.get_model("tickets", "Ticket")
    Message = apps.get_model("tickets", "Message")
    for t in Ticket.objects.all().iterator():
        last = (Message.objects.filter(ticket=t, is_draft=False)
                .order_by("-created_at").values_list("created_at", flat=True).first())
        t.last_activity_at = last or t.updated_at or t.created_at
        t.save(update_fields=["last_activity_at"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0026_ticket_agent_unread_ticket_last_activity_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
