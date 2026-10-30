import pytest

from app import create_app
from app.agents.providers import ProviderResponse, ToolCall
from app.config import TestConfig
from app.extensions import db
from app.models import User
from data.seed.seed import seed_database

# Test-only passwords for the throw-away in-memory test database. The real application generates random ones.
PASSWORDS = {"student_a": "Tst-Pass#A-9x1", "student_b": "Tst-Pass#B-9x1", "student_c": "Tst-Pass#C-9x1", "admin": "Tst-Pass#Adm-9x1"}


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        seed_database(PASSWORDS)
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def student_a(app):
    return User.query.filter_by(username="student_a").first()


@pytest.fixture()
def student_b(app):
    return User.query.filter_by(username="student_b").first()


class ScriptedProvider:
    """Test helper: replays a fixed list of ProviderResponse objects / exceptions (not an LLM)."""
    name = "scripted"
    model = "scripted-test"

    def __init__(self, steps):
        self.steps, self.calls, self.seen_tools = list(steps), 0, None

    def chat(self, messages, tools):
        self.calls += 1
        self.seen_tools = tools
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def tool_call_response(name, args="{}", cid="c1"):
    return ProviderResponse("", [ToolCall(cid, name, args)], usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def text_response(text, usage=True):
    return ProviderResponse(text, [], usage={"input_tokens": 12, "output_tokens": 6, "total_tokens": 18} if usage else None)


@pytest.fixture()
def use_provider(app):
    def _set(provider):
        app.extensions["provider"] = provider
        return provider
    return _set




def login(client, username):
    r = client.post("/login", data={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 302
    return client


class Browser:
    """A separate cookie jar per simulated person. Clears Flask-Login's per-context user cache before each request so
    a student and an administrator can be used side by side inside one test."""

    def __init__(self, app, username=None):
        self.c = app.test_client()
        if username:
            r = self.post("/login", data={"username": username, "password": PASSWORDS[username]})
            assert r.status_code == 302, f"login failed for {username}: {r.status_code}"

    def req(self, method, url, **kw):
        from flask import g
        g.pop("_login_user", None)
        return getattr(self.c, method)(url, **kw)

    def get(self, url, **kw):
        return self.req("get", url, **kw)

    def post(self, url, **kw):
        return self.req("post", url, **kw)

    def delete(self, url, **kw):
        return self.req("delete", url, **kw)


@pytest.fixture()
def student(app):
    return Browser(app, "student_a")


@pytest.fixture()
def other_student(app):
    return Browser(app, "student_b")


@pytest.fixture()
def admin(app):
    return Browser(app, "admin")
