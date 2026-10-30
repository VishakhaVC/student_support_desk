import pytest

from app.models import ApprovalRequest, AuditLog, StudentProfile, User
from app.security.authz import PermissionDenied
from app.services import approvals
from app.services.chat import handle_chat
from tests.conftest import login

OLD_EMAIL = "aarav.mehta@students.example.edu"


def email(sid="S2026001"):
    return StudentProfile.query.filter_by(student_id=sid).first().email


def phone(sid="S2026001"):
    return StudentProfile.query.filter_by(student_id=sid).first().phone


@pytest.fixture()
def admin(app):
    return User.query.filter_by(username="admin").first()


def request_change(student, text="Change my email to new.aarav@example.com"):
    return handle_chat(student, text)


def test_s5_sensitive_change_creates_pending_request_and_does_not_execute(app, student_a):
    r = request_change(student_a)
    assert "pending" in r["reply"].lower()
    req = ApprovalRequest.query.one()
    assert req.status == "pending" and req.executed_at is None and email() == OLD_EMAIL
    a = AuditLog.query.filter_by(action="request_contact_change").one()
    assert a.decision == "pending" and a.trace_id == r["trace_id"] and "new.aarav@example.com" not in (a.details or "")


def test_s6_admin_approval_applies_change_exactly_once(app, student_a, admin):
    request_change(student_a)
    req = ApprovalRequest.query.one()
    approvals.decide(admin, req.id, True)
    assert email() == "new.aarav@example.com" and req.status == "approved" and req.executed_at is not None
    with pytest.raises(approvals.ApprovalStateError):                 # replay of a completed approval
        approvals.decide(admin, req.id, True)
    assert AuditLog.query.filter_by(action="decide_approval", decision="approved").count() == 1
    assert AuditLog.query.filter_by(action="decide_approval", decision="denied").count() == 1


def test_s7_rejection_never_executes(app, student_a, admin):
    request_change(student_a, "Update my phone to +91 98765 43210")
    req = ApprovalRequest.query.one()
    approvals.decide(admin, req.id, False, "not verified")
    assert req.status == "rejected" and req.executed_at is None and phone() == "+91 90000 10001"
    with pytest.raises(approvals.ApprovalStateError):                  # cannot approve a rejected request later
        approvals.decide(admin, req.id, True)
    assert phone() == "+91 90000 10001"


def test_s8_student_cannot_approve_via_api_or_service(app, client, student_a):
    request_change(student_a)
    req = ApprovalRequest.query.one()
    login(client, "student_a")
    assert client.post(f"/api/approvals/{req.id}/approve").status_code == 403
    assert client.post(f"/admin/approvals/{req.id}/decide", data={"decision": "approve"}).status_code == 403
    with pytest.raises(PermissionDenied):
        approvals.decide(student_a, req.id, True)
    assert req.status == "pending" and email() == OLD_EMAIL


def test_admin_can_approve_through_http(app, client, student_a):
    request_change(student_a)
    req = ApprovalRequest.query.one()
    login(client, "admin")
    r = client.post(f"/api/approvals/{req.id}/approve")
    assert r.status_code == 200 and r.get_json()["executed"] is True and email() == "new.aarav@example.com"
    assert client.post(f"/api/approvals/{req.id}/approve").status_code == 409        # replay over HTTP


def test_admin_dashboard_form_approves_and_rejects(app, client, student_a):
    request_change(student_a)
    login(client, "admin")
    req = ApprovalRequest.query.one()
    client.post(f"/admin/approvals/{req.id}/decide", data={"decision": "reject", "note": "not verified"})
    assert req.status == "rejected" and email() == OLD_EMAIL


def test_self_approval_is_blocked(app, admin):
    from app.extensions import db
    req = ApprovalRequest(approval_ref="APR-9001", requester_user_id=admin.id, student_id="S2026001", field="email", new_value="x@example.com")
    db.session.add(req); db.session.commit()
    with pytest.raises(PermissionDenied) as e:
        approvals.decide(admin, req.id, True)
    assert e.value.reason == "self_approval_not_allowed" and req.status == "pending" and email() == OLD_EMAIL


def test_duplicate_pending_requests_are_collapsed(app, student_a):
    request_change(student_a)
    request_change(student_a)
    assert ApprovalRequest.query.count() == 1


def test_request_cannot_be_forged_to_target_another_student(app, student_a, student_b):
    request_change(student_a)
    assert ApprovalRequest.query.one().student_id == "S2026001"            # derived from the session, not from the message


def test_execution_failure_rolls_back_and_keeps_request_pending(app, student_a, admin):
    request_change(student_a)
    req = ApprovalRequest.query.one()
    req.field = "cgpa"                                                    # tampered request: no longer a permitted field
    from app.extensions import db
    db.session.commit()
    with pytest.raises(ValueError):
        approvals.decide(admin, req.id, True)
    db.session.refresh(req)
    assert req.status == "pending" and req.executed_at is None
    assert AuditLog.query.filter_by(decision="error", action="decide_approval").count() == 1


def test_students_see_only_their_own_approvals(app, client, student_a, student_b):
    request_change(student_a)
    login(client, "student_b")
    assert client.get("/api/approvals").get_json() == []
    assert b"APR-0001" not in client.get("/approvals").data
