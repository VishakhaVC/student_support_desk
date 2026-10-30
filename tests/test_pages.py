import json
import re
from pathlib import Path

import pytest

from app.models import ApprovalRequest, AuditLog, MemoryItem, StudentProfile
from tests.conftest import Browser, PASSWORDS

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"assignment|deterministic|test double|rule[- ]based|mock|prototype|\bdemo\b|demonstration|simulat|not real|fictional|classroom", re.I)
STUDENT_PAGES = ["/dashboard", "/assistant", "/tickets", "/tickets?status=active", "/tickets/TKT-0001", "/approvals", "/profile"]
ADMIN_PAGES = ["/admin/", "/admin/students", "/admin/students/new", "/admin/students/S2026001", "/admin/tickets", "/admin/tickets/TKT-0001",
               "/admin/approvals", "/admin/approvals?status=all", "/admin/performance", "/admin/logs"]


def visible_text(html: bytes) -> str:
    t = re.sub(rb"<(script|style)\b.*?</\1>", b"", html, flags=re.S)
    return re.sub(rb"<[^>]+>", b" ", t).decode("utf-8", "ignore")


def test_login_page_is_clean_and_shows_no_credentials(app):
    b = Browser(app)
    html = b.get("/login").data
    text = visible_text(html)
    for secret in PASSWORDS.values():
        assert secret not in text
    for name in ("student_a", "student_b", "admin"):
        assert name not in text
    assert b'name="username"' in html and b'name="password"' in html and not FORBIDDEN.search(text)
    assert "Local demo" not in text and "demo accounts" not in text.lower()


@pytest.mark.parametrize("who,pages", [("student_a", STUDENT_PAGES), ("admin", ADMIN_PAGES)])
def test_every_page_renders_without_internal_terminology(app, who, pages):
    b = Browser(app, who)
    for url in pages:
        r = b.get(url)
        assert r.status_code == 200, url
        bad = FORBIDDEN.search(visible_text(r.data))
        assert bad is None, f"{url}: found '{bad.group(0)}'"


def test_javascript_and_templates_have_no_internal_terminology():
    for f in list((ROOT / "app/static").glob("*.js")) + list((ROOT / "app/templates").glob("*.html")):
        assert not re.search(r"assignment|deterministic|test double|prototype|mock model", f.read_text(encoding="utf-8"), re.I), f.name


@pytest.mark.parametrize("who,pages", [("student_a", STUDENT_PAGES), ("admin", ADMIN_PAGES)])
def test_no_dead_links_and_all_forms_point_to_real_routes(app, who, pages):
    b = Browser(app, who)
    routes = {r.rule for r in app.url_map.iter_rules()}
    seen = set()
    for url in pages:
        html = b.get(url).data.decode()
        for href in re.findall(r'<a[^>]+href="(/[^"#]*)', html):
            if href in seen or href.startswith("/static"):
                continue
            seen.add(href)
            assert b.get(href).status_code in (200, 302), f"dead link {href} on {url}"
        for action in re.findall(r'<form[^>]+action="(/[^"]+)"', html):
            path = re.sub(r"/(TKT-\d+|S\d+|\d+)(?=/|$)", "/<x>", action)
            assert any(re.sub(r"<[^>]+>", "<x>", r) == path for r in routes), f"form posts to unknown route {action}"


def test_navigation_matches_the_required_modules(app):
    s = visible_text(Browser(app, "student_a").get("/dashboard").data)
    for item in ("Dashboard", "AI Assistant", "Support Requests", "Approvals", "My Profile"):
        assert item in s
    a = visible_text(Browser(app, "admin").get("/admin/").data)
    for item in ("Dashboard", "Students", "Support Requests", "Approvals", "AI Performance", "Activity Logs"):
        assert item in a
    assert "Evaluation" not in a and "Agent runs" not in a


def test_old_admin_urls_redirect(app, admin):
    assert admin.get("/admin/audit").status_code == 302 and admin.get("/admin/evaluation").status_code == 302 and admin.get("/admin/runs").status_code == 302


def test_dashboard_values_come_from_the_database(app, student, admin):
    d = visible_text(student.get("/dashboard").data)
    assert "150,000" in d and "Merit" in d and "Aarav" in d
    from app.extensions import db
    from app.models import FeeRecord
    f = FeeRecord.query.filter_by(student_id="S2026001").one()
    f.paid = 450000; f.status = "Paid"; db.session.commit()
    assert "INR 0" in visible_text(student.get("/dashboard").data) and "Paid" in visible_text(student.get("/dashboard").data)


def test_pending_approval_flow_is_visible_everywhere_and_profile_updates_after_approval(app, student, admin):
    student.post("/profile/contact-change", data={"field": "email", "new_value": "aarav.fresh@example.com"})
    a = ApprovalRequest.query.one()
    assert "Pending approvals" in visible_text(student.get("/dashboard").data)
    assert re.search(rb'Pending approvals</div><div class="value num">1<', student.get("/dashboard").data)
    prof = visible_text(student.get("/profile").data)
    assert "change pending" in prof and "aarav.mehta@students.example.edu" in prof
    assert "aarav.fresh@example.com" in visible_text(student.get("/approvals").data)
    assert b"aarav.fresh@example.com" in admin.get("/admin/approvals").data and re.search(rb'id="nav-approvals">1<', admin.get("/admin/").data)
    r = admin.post(f"/api/approvals/{a.id}/approve", json={"note": "verified by registrar"})
    assert r.status_code == 200 and r.get_json()["executed"] is True
    assert "aarav.fresh@example.com" in visible_text(student.get("/profile").data) and "change pending" not in visible_text(student.get("/profile").data)
    ap = visible_text(student.get("/approvals").data)
    assert "Applied: registered email updated" in ap and "verified by registrar" in ap
    assert StudentProfile.query.filter_by(student_id="S2026001").one().email == "aarav.fresh@example.com"
    assert admin.post(f"/api/approvals/{a.id}/approve").status_code == 409


def test_rejection_requires_reason_and_leaves_record_unchanged(app, student, admin):
    student.post("/profile/contact-change", data={"field": "phone", "new_value": "+91 98765 43210"})
    a = ApprovalRequest.query.one()
    assert admin.post(f"/api/approvals/{a.id}/reject", json={}).status_code == 400
    admin.post(f"/admin/approvals/{a.id}/decide", data={"decision": "reject", "note": ""})
    assert a.status == "pending"
    assert admin.post(f"/api/approvals/{a.id}/reject", json={"note": "Could not verify the number"}).status_code == 200
    assert a.status == "rejected" and a.executed_at is None and StudentProfile.query.filter_by(student_id="S2026001").one().phone == "+91 90000 10001"
    ap = visible_text(student.get("/approvals").data)
    assert "Could not verify the number" in ap and "Not applied" in ap


def test_approvals_admin_filters(app, student, admin):
    student.post("/profile/contact-change", data={"field": "email", "new_value": "aarav.fresh@example.com"})
    assert b"APR-0001" in admin.get("/admin/approvals?status=pending").data and b"APR-0001" not in admin.get("/admin/approvals?status=approved").data
    assert b"APR-0001" in admin.get("/admin/approvals?status=all&q=aarav").data


def test_student_cannot_open_admin_approvals_page_or_decide(app, student):
    assert student.get("/admin/approvals").status_code == 403
    student.post("/profile/contact-change", data={"field": "email", "new_value": "aarav.fresh@example.com"})
    assert student.post(f"/admin/approvals/{ApprovalRequest.query.one().id}/decide", data={"decision": "approve"}).status_code == 403


def test_preferences_can_be_edited_and_are_used(app, student):
    student.post("/profile/preferences", data={"preferred_language": "Hindi", "response_style": "short", "preferred_name": "Aaru"})
    items = {m.key: m.value for m in MemoryItem.query.all()}
    assert items == {"preferred_language": "Hindi", "response_style": "short", "preferred_name": "Aaru"}
    assert b'value="Aaru"' in student.get("/profile").data
    student.post("/profile/preferences", data={"preferred_language": "", "response_style": "", "preferred_name": ""})
    assert MemoryItem.query.count() == 0
    student.post("/profile/preferences", data={"preferred_language": "Hindi", "preferred_name": "x<script>"})
    assert MemoryItem.query.filter_by(key="preferred_name").count() == 0
    assert AuditLog.query.filter_by(action="update_preferences").count() >= 2


def test_conversation_clear_actions(app, student, other_student):
    c = student.post("/api/chat", json={"message": "hello there"}).get_json()["conversation_id"]
    o = other_student.post("/api/chat", json={"message": "hi"}).get_json()["conversation_id"]
    assert student.delete(f"/api/conversations/{o}").status_code == 404                     # not mine
    assert student.delete(f"/api/conversations/{c}").status_code == 200
    assert student.get("/api/conversations").get_json() == []
    assert len(other_student.get("/api/conversations").get_json()) == 1                     # untouched
    student.post("/api/chat", json={"message": "again"})
    assert student.delete("/api/conversations").get_json()["deleted"] == 1


# ---------------------------------------------------------------- AI Performance
def test_performance_page_with_no_data_says_not_available_and_invents_nothing(app, admin, tmp_path):
    app.config["RESULTS_DIR"] = tmp_path
    text = visible_text(admin.get("/admin/performance").data)
    assert "No conversations measured yet" in text and "No model evaluation has been run yet" in text
    assert not re.search(r"\d+%", text)


def test_performance_separates_live_model_results_from_workflow_checks(app, admin, tmp_path):
    def write(label, provider, passed):
        recs = [{"test_id": "G1", "question": "q", "success": passed == 1, "failed_checks": ["no tool"] if passed == 0 else []}]
        s = {"label": label, "provider": provider, "model": "openai/gpt-oss-120b" if provider == "groq" else "workflow-checks", "generated_utc": "2026-10-29 10:00:00",
             "cases": 1, "passed": passed, "accuracy": passed, "tool_selection_rate": 0.5 if provider == "groq" else 1.0, "avg_latency_ms": 1500.0, "total_tokens": 900 if provider == "groq" else None,
             "input_tokens": 700 if provider == "groq" else None, "output_tokens": 200 if provider == "groq" else None, "by_category": {}}
        (tmp_path / f"results_{label}.json").write_text(json.dumps({"summary": s, "records": recs}))
    write("zz_live", "groq", 0)
    write("zz_flow", "deterministic", 1)
    app.config["RESULTS_DIR"] = tmp_path
    html = admin.get("/admin/performance").data
    text = visible_text(html)
    evaluation, workflow = text.split("Evaluation results")[1].split("Workflow checks")[0], text.split("Workflow checks")[1]
    assert "gpt-oss-120b" in evaluation and "900" in evaluation and "no tool" in evaluation and "100%" not in evaluation
    assert "without calling the language model" in workflow and "gpt-oss" not in workflow
    assert not FORBIDDEN.search(text)


def test_live_usage_counts_only_real_model_runs(app, admin, student_a):
    from app.extensions import db
    from app.models import AgentRun
    import uuid
    def run(provider, status, tokens):
        db.session.add(AgentRun(trace_id=uuid.uuid4().hex, user_id=student_a.id, provider=provider, model="m", status=status, latency_ms=2000.0,
                                input_tokens=tokens[0], output_tokens=tokens[1], total_tokens=tokens[2]))
    run("groq", "completed", (100, 20, 120)); run("groq", "error", (None, None, None)); run("deterministic", "completed", (None, None, None))
    db.session.commit()
    text = visible_text(admin.get("/admin/performance").data)
    assert re.search(r"Queries handled\s+2\b", text) and re.search(r"Success rate\s+50%", text) and "120" in text


def test_comparison_and_requirement_documents_remain_available():
    for f in ("docs/ASSIGNMENT_COMPARISON.md", "docs/REQUIREMENTS_MATRIX.md", "docs/ASSIGNMENT_1.md", "docs/ASSIGNMENT_2.md",
              "docs/VIDEO_SCRIPT_ASSIGNMENT_1.md", "docs/VIDEO_SCRIPT_ASSIGNMENT_2.md", "evaluations/golden_dataset/golden.json", "data/sample_policy/student_handbook.md"):
        assert (ROOT / f).exists(), f


def test_gitignore_protects_secrets():
    ignore = (ROOT / ".gitignore").read_text()
    for entry in (".env", "login.txt", "*.db", "instance/"):
        assert entry in ignore


def test_web_app_refuses_non_live_model_mode():
    from app import create_app
    from app.config import Config
    class Prod(Config):
        TESTING = False
        LLM_MODE = "deterministic"
    with pytest.raises(RuntimeError):
        create_app(Prod)
