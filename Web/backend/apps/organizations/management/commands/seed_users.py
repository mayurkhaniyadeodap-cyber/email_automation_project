"""
Seed the example login users with roles (idempotent).

    python manage.py seed_users
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.organizations.models import Organization, UserProfile

USERS = [
    # username, name, email, password, role
    ("admin", "Admin", "admin@deodap.com", "Admin@123", UserProfile.ROLE_ADMIN),
    ("agent1", "Agent One", "agent1@deodap.com", "Agent@123", UserProfile.ROLE_AGENT),
    ("agent2", "Agent Two", "agent2@deodap.com", "Agent@123", UserProfile.ROLE_AGENT),
]


class Command(BaseCommand):
    help = "Create/update the example login users with roles."

    def handle(self, *args, **opts):
        User = get_user_model()
        orgs = list(Organization.objects.all())
        for username, name, email, password, role in USERS:
            user, _ = User.objects.get_or_create(
                username=username, defaults={"email": email})
            user.email = email
            user.is_active = True
            user.is_staff = role == UserProfile.ROLE_ADMIN
            user.is_superuser = username == "admin"
            user.set_password(password)          # Django secure hashing
            user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.name, profile.role = name, role
            profile.failed_attempts, profile.is_locked = 0, False
            profile.save()
            # Give each user access to all existing orgs so they can see data.
            if orgs and hasattr(user, "organizations"):
                user.organizations.add(*orgs)
            self.stdout.write(f"  {username:8} role={role} ({'locked reset' if True else ''})")
        self.stdout.write(self.style.SUCCESS("Seeded example users."))
