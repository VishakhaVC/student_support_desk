"""Regressions: protected name changes actually apply; password changes remain private."""
from werkzeug.security import check_password_hash
from app.extensions import db
from app.models import ApprovalRequest, StudentProfile, User, SupportTicket
from app.services import approvals, tickets
from tests.conftest import PASSWORDS


def test_name_change_applies_only_after_approval(app):
    with app.app_context():
        student = User.query.filter_by(username="student_a").first()
        admin = User.query.filter_by(username="admin").first()
        profile = StudentProfile.query.filter_by(student_id=student.student_id).first()
        initial = profile.full_name
        request = approvals.request_contact_change(student, "full_name", "Aarav Updated", "Correct name", None)
        assert profile.full_name == initial
        approved = ApprovalRequest.query.filter_by(approval_ref=request["approval_ref"]).one()
        approvals.decide(admin, approved.id, True, "Verified identity")
        db.session.refresh(profile)
        db.session.refresh(student)
        assert profile.full_name == "Aarav Updated"
        assert student.display_name == "Aarav Updated"
        assert approved.executed_at is not None


def test_rejected_name_change_never_applies(app):
    with app.app_context():
        student = User.query.filter_by(username="student_a").first()
        admin = User.query.filter_by(username="admin").first()
        profile = StudentProfile.query.filter_by(student_id=student.student_id).first()
        before = profile.full_name
        request = approvals.request_contact_change(student, "full_name", "Aarav Rejected", None, None)
        approval = ApprovalRequest.query.filter_by(approval_ref=request["approval_ref"]).one()
        approvals.decide(admin, approval.id, False, "Identity check unsuccessful")
        db.session.refresh(profile)
        assert profile.full_name == before
        assert approval.executed_at is None


def test_linked_ticket_cannot_be_closed_before_approval(app):
    with app.app_context():
        student = User.query.filter_by(username="student_a").first()
        admin = User.query.filter_by(username="admin").first()
        created = tickets.create_ticket(student, "contact_change", "Correct registered name", "Please correct the name in my record")
        ticket = SupportTicket.query.filter_by(ticket_ref=created["ticket_ref"]).one()
        approvals.request_contact_change(student, "full_name", "Aarav Updated", None, None, ticket_id=ticket.id)
        from pytest import raises
        with raises(tickets.TicketError) as err:
            tickets.admin_update(admin, ticket.ticket_ref, "Resolved", "Done")
        assert err.value.code == "approval_required"
        assert ticket.status == "Open"


def test_student_password_change(client, app):
    response = client.post("/login", data={"username": "student_a", "password": PASSWORDS["student_a"]})
    assert response.status_code == 302
    changed = client.post("/profile/password", data={
        "current_password": PASSWORDS["student_a"],
        "new_password": "ChangedPassword#A123", "confirm_password": "ChangedPassword#A123"})
    assert changed.status_code == 302
    with app.app_context():
        student = User.query.filter_by(username="student_a").first()
        assert check_password_hash(student.password_hash, "ChangedPassword#A123")
        assert not check_password_hash(student.password_hash, PASSWORDS["student_a"])
