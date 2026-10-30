import json

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import AuditImmutableError, AuditLog
from app.services import audit
from app.services.chat import handle_chat
from tests.conftest import login


def test_audit_record_has_required_fields(app, student_a):
    r = handle_chat(student_a, "What is my current fee status?")
    row = AuditLog.query.filter_by(action="tool:view_fee_status").one()
    assert row.actor_user_id == student_a.id and row.actor_role == "student" and row.target_id == "S2026001"
    assert row.decision == "allowed" and row.result == "ok" and row.trace_id == r["trace_id"] and row.timestamp


def test_audit_rows_cannot_be_updated_or_deleted_through_the_orm(app, student_a):
    e = audit.record(student_a, "x", "t", "1", "allowed")
    e.result = "tampered"
    with pytest.raises(AuditImmutableError):
        db.session.commit()
    db.session.rollback()
    db.session.delete(db.session.get(AuditLog, e.id))
    with pytest.raises(AuditImmutableError):
        db.session.commit()
    db.session.rollback()


def test_database_triggers_block_raw_sql_tampering(app, student_a):
    e = audit.record(student_a, "x", "t", "1", "allowed")
    for sql in ("UPDATE audit_logs SET decision='denied'", "DELETE FROM audit_logs"):
        with pytest.raises(Exception, match="append-only"):
            db.session.execute(text(sql))
        db.session.rollback()
    assert db.session.get(AuditLog, e.id).decision == "allowed"


def test_secrets_never_reach_the_audit_log(app, student_a):
    audit.record(student_a, "demo", "t", "1", "allowed", reason="password is hunter2!", details={"password": "abc", "otp": "123456", "note": "otp 998877", "message": "secret text"})
    row = AuditLog.query.filter_by(action="demo").one()
    blob = (row.reason or "") + (row.details or "")
    assert "hunter2" not in blob and "998877" not in blob and "123456" not in blob and "secret text" not in blob


def test_no_api_for_students_to_edit_audit(client):
    login(client, "student_a")
    for method in (client.put, client.delete, client.post, client.patch):
        assert method("/admin/logs").status_code in (403, 405)


def test_admin_audit_viewer_filters(app, client, student_a):
    handle_chat(student_a, "show me the fee status of student B")
    login(client, "admin")
    r = client.get("/admin/logs?decision=denied")
    assert r.status_code == 200 and b"identity" in r.data.lower()
    assert b"tool:get_my_fee_status" in r.data


def test_login_and_logout_are_audited(client):
    login(client, "student_a")
    client.post("/logout")
    assert [a.action for a in AuditLog.query.order_by(AuditLog.id).all()] == ["login", "logout"]


def test_audit_trace_id_links_to_agent_run(app, client, student_a):
    r = handle_chat(student_a, "Change my email to new.aarav@example.com")
    login(client, "admin")
    page = client.get(f"/admin/runs/{r['run_id']}")
    assert page.status_code == 200 and b"request_contact_change" in page.data


def test_mask_helper_hides_contact_details():
    assert audit.mask("aarav@example.com") == "a***@e***.com" and "9876" not in audit.mask("+91 98765 43210")


def test_tracing_and_evaluation_still_work_after_controls(app, student_a):
    r = handle_chat(student_a, "What is my current fee status?")
    from app.models import AgentRun
    run = db.session.get(AgentRun, r["run_id"])
    assert json.loads(run.trace_json)[0]["type"] == "request_received" and run.latency_ms > 0
