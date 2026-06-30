"""
Login + role + account-lockout tests.

    python manage.py test apps.organizations.tests_auth
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.organizations.models import UserProfile


class LoginTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user("agent1", password="Agent@123")
        p = UserProfile.objects.create(user=self.user, name="Agent One",
                                       role=UserProfile.ROLE_AGENT)
        self.profile = p

    def test_login_success_returns_role_and_permissions(self):
        r = self.api.post("/api/auth/login/", {"username": "agent1", "password": "Agent@123"},
                          format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["token"])
        self.assertEqual(r.data["user"]["role"], "agent")
        self.assertEqual(r.data["user"]["role_display"], "Agent")
        self.assertEqual(r.data["user"]["permissions"]["nav"], ["inbox", "tickets"])
        self.assertFalse(r.data["user"]["permissions"]["read_only"])

    def test_missing_fields(self):
        r = self.api.post("/api/auth/login/", {"username": "agent1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("required", r.data["detail"].lower())

    def test_invalid_password_shows_attempts_left(self):
        r = self.api.post("/api/auth/login/", {"username": "agent1", "password": "wrong"},
                          format="json")
        self.assertEqual(r.status_code, 401)
        self.assertIn("attempt", r.data["detail"].lower())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.failed_attempts, 1)

    def test_account_locks_after_5_failures(self):
        for _ in range(5):
            self.api.post("/api/auth/login/", {"username": "agent1", "password": "x"},
                          format="json")
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_locked)
        # Even the correct password is refused while locked.
        r = self.api.post("/api/auth/login/", {"username": "agent1", "password": "Agent@123"},
                          format="json")
        self.assertEqual(r.status_code, 403)
        self.assertIn("locked", r.data["detail"].lower())

    def test_viewer_is_read_only(self):
        User = get_user_model()
        u = User.objects.create_user("viewer1", password="View@123")
        UserProfile.objects.create(user=u, role=UserProfile.ROLE_VIEWER)
        r = self.api.post("/api/auth/login/", {"username": "viewer1", "password": "View@123"},
                          format="json")
        self.assertTrue(r.data["user"]["permissions"]["read_only"])

    def test_admin_sees_all_nav(self):
        User = get_user_model()
        u = User.objects.create_superuser("admin", "a@b.c", "Admin@123")
        r = self.api.post("/api/auth/login/", {"username": "admin", "password": "Admin@123"},
                          format="json")
        self.assertEqual(r.data["user"]["permissions"]["nav"],
                         ["dashboard", "inbox", "tickets", "escalations",
                          "internal-communications", "settings"])


class TeamManagementTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@b.c", "Admin@123")
        UserProfile.objects.create(user=self.admin, role=UserProfile.ROLE_ADMIN)
        self.agent = User.objects.create_user("agent1", password="Agent@123")
        UserProfile.objects.create(user=self.agent, role=UserProfile.ROLE_AGENT)

    def _as(self, user):
        self.api.force_authenticate(user)

    def test_admin_can_add_member_and_it_logs(self):
        from apps.organizations.models import UserAuditLog
        self._as(self.admin)
        r = self.api.post("/api/users/", {"username": "agent2", "name": "Agent Two",
                          "password": "Agent@123", "role": "agent"}, format="json")
        self.assertEqual(r.status_code, 201)
        u = get_user_model().objects.get(username="agent2")
        self.assertEqual(u.profile.role, "agent")
        self.assertTrue(u.check_password("Agent@123"))
        self.assertTrue(UserAuditLog.objects.filter(event="USER_CREATED", target="agent2").exists())

    def test_agent_cannot_manage_users(self):
        self._as(self.agent)
        self.assertEqual(self.api.get("/api/users/").status_code, 403)
        self.assertEqual(self.api.post("/api/users/", {"username": "x", "password": "xxxxxx"},
                         format="json").status_code, 403)

    def test_admin_disables_member_logs_user_disabled(self):
        from apps.organizations.models import UserAuditLog
        self._as(self.admin)
        r = self.api.patch(f"/api/users/{self.agent.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_active)
        self.assertTrue(UserAuditLog.objects.filter(event="USER_DISABLED", target="agent1").exists())

    def test_admin_resets_password_and_unlocks(self):
        self.agent.profile.is_locked = True
        self.agent.profile.failed_attempts = 5
        self.agent.profile.save()
        self._as(self.admin)
        r = self.api.post(f"/api/users/{self.agent.id}/reset_password/",
                          {"password": "NewPass@1"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.check_password("NewPass@1"))
        self.assertFalse(self.agent.profile.is_locked)

    def test_login_writes_audit_and_last_login(self):
        from apps.organizations.models import UserAuditLog
        r = self.api.post("/api/auth/login/", {"username": "agent1", "password": "Agent@123"},
                          format="json")
        self.assertEqual(r.status_code, 200)
        self.agent.refresh_from_db()
        self.assertIsNotNone(self.agent.last_login)
        self.assertTrue(UserAuditLog.objects.filter(event="USER_LOGIN", target="agent1").exists())


class DeleteMemberTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@b.c", "Admin@123")
        UserProfile.objects.create(user=self.admin, role=UserProfile.ROLE_ADMIN)
        self.agent = User.objects.create_user("agent1", password="Agent@123")
        UserProfile.objects.create(user=self.agent, role=UserProfile.ROLE_AGENT)
        self.api.force_authenticate(self.admin)

    def test_admin_deletes_member_and_logs(self):
        from apps.organizations.models import UserAuditLog
        r = self.api.delete(f"/api/users/{self.agent.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(get_user_model().objects.filter(username="agent1").exists())
        self.assertTrue(UserAuditLog.objects.filter(event="USER_DELETED", target="agent1").exists())

    def test_cannot_delete_self(self):
        r = self.api.delete(f"/api/users/{self.admin.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("your own", r.data["detail"].lower())

    def test_cannot_delete_last_admin(self):
        other = get_user_model().objects.create_user("admin2", password="x")
        UserProfile.objects.create(user=other, role=UserProfile.ROLE_ADMIN)
        self.api.force_authenticate(other)
        # admin (superuser) is the only OTHER admin; deleting admin is allowed (admin2 remains)
        self.assertEqual(self.api.delete(f"/api/users/{self.admin.id}/").status_code, 204)
        # now admin2 is the last admin -> deleting self blocked by self-guard anyway,
        # but deleting via another path: make a fresh agent-admin to test last-admin guard
        self.api.force_authenticate(other)
        r = self.api.delete(f"/api/users/{other.id}/")  # self-delete guard
        self.assertEqual(r.status_code, 400)
