from app.models import AuditLog
from tests.conftest import PASSWORDS, login


def test_anonymous_users_are_sent_to_login(client):
    assert client.get("/dashboard").status_code == 302
    assert client.get("/api/memory").status_code in (302, 401)
    assert client.post("/api/chat", json={"message": "hi"}).status_code in (302, 401)


def test_wrong_password_is_rejected_and_audited(client):
    r = client.post("/login", data={"username": "student_a", "password": "wrong"})
    assert r.status_code == 401
    row = AuditLog.query.filter_by(action="login", decision="denied").first()
    assert row and row.actor_role == "anonymous" and "wrong" not in (row.details or "") + (row.reason or "")


def test_unknown_user_gets_same_response_as_wrong_password(client):
    a = client.post("/login", data={"username": "nobody", "password": "x"})
    b = client.post("/login", data={"username": "student_a", "password": "x"})
    assert a.status_code == b.status_code == 401 and b"Invalid username or password" in a.data and b"Invalid username or password" in b.data


def test_passwords_are_stored_hashed(app):
    from app.models import User
    for u in User.query.all():
        assert u.password_hash != PASSWORDS[u.username] and u.password_hash.startswith(("scrypt:", "pbkdf2:"))


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        client.post("/login", data={"username": "student_b", "password": "bad"})
    r = client.post("/login", data={"username": "student_b", "password": PASSWORDS["student_b"]})
    assert r.status_code == 429


def test_logout_requires_post_and_ends_session(client):
    login(client, "student_a")
    assert client.get("/logout").status_code == 405
    client.post("/logout")
    assert client.get("/dashboard").status_code == 302


def test_students_cannot_open_admin_pages(client):
    login(client, "student_a")
    for url in ("/admin/", "/admin/students", "/admin/tickets", "/admin/approvals", "/admin/performance", "/admin/logs"):
        assert client.get(url).status_code == 403
    assert AuditLog.query.filter_by(decision="denied", actor_role="student").count() >= 4


def test_admin_can_open_admin_pages_but_not_student_pages(client):
    login(client, "admin")
    for url in ("/admin/", "/admin/students", "/admin/tickets", "/admin/approvals", "/admin/performance", "/admin/logs"):
        assert client.get(url).status_code == 200
    assert client.get("/dashboard").status_code == 403


def test_csrf_is_enforced_when_enabled():
    from app import create_app
    from app.config import TestConfig
    from app.extensions import db
    from data.seed.seed import seed_database
    app = create_app(TestConfig, WTF_CSRF_ENABLED=True)
    with app.app_context():
        db.create_all(); seed_database(PASSWORDS)
        c = app.test_client()
        assert c.post("/login", data={"username": "student_a", "password": PASSWORDS["student_a"]}).status_code == 400   # no token


def test_session_cookie_flags(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True and app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
