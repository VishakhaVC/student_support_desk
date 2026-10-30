import re

import pytest

from app.models import ApprovalRequest, AuditLog, StudentProfile, SupportTicket, TicketEvent
from app.services import tickets
from app.services.chat import handle_chat

FORM = {"category": "payment", "subject": "Portal failed twice", "description": "The payment portal failed twice yesterday evening."}


def create(student, **over):
    return student.post("/tickets", data={**FORM, **over})


def ref_of(r):
    return re.search(r"/tickets/(TKT-\d+)", r.headers["Location"]).group(1)


def test_student_creates_request_and_it_is_persisted(app, student):
    r = create(student)
    assert r.status_code == 302
    t = SupportTicket.query.filter_by(ticket_ref=ref_of(r)).one()
    assert t.student_id == "S2026001" and t.status == "Open" and t.source == "web" and t.created_at and t.updated_at
    assert TicketEvent.query.filter_by(ticket_id=t.id, event_type="created").count() == 1
    assert AuditLog.query.filter_by(action="create_support_ticket", target_id=t.ticket_ref).count() == 1
    page = student.get("/tickets").data
    assert t.ticket_ref.encode() in page and b"Portal failed twice" in page


def test_duplicate_submission_is_handled(app, student):
    first = ref_of(create(student))
    second = create(student)
    assert ref_of(second) == first and SupportTicket.query.filter_by(student_id="S2026001").count() == 2     # seed + 1


def test_invalid_request_is_rejected(app, student):
    before = SupportTicket.query.count()
    create(student, subject="x")
    create(student, category="hacking")
    create(student, description="short")
    assert SupportTicket.query.count() == before


def test_admin_sees_the_request(app, student, admin):
    ref = ref_of(create(student))
    page = admin.get("/admin/tickets").data
    assert ref.encode() in page and b"Aarav Mehta" in page
    assert ref.encode() in admin.get("/admin/tickets?status=Open").data
    assert ref.encode() in admin.get(f"/admin/tickets/{ref}").data
    assert b"payment portal failed twice" in admin.get(f"/admin/tickets/{ref}").data.lower()


def test_full_lifecycle_review_respond_resolve_visible_to_student(app, student, admin):
    ref = ref_of(create(student))
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Under Review"})
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    assert t.status == "Under Review"
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Awaiting Student", "note": "Please send the transaction reference number."})
    assert t.status == "Awaiting Student" and t.last_response.startswith("Please send")
    page = student.get(f"/tickets/{ref}").data
    assert b"Awaiting Student" in page and b"Please send the transaction reference" in page and b"Reply to administration" in page
    student.post(f"/tickets/{ref}/reply", data={"message": "The reference is TXN778899."})
    assert t.status == "Under Review"                                              # reply moves it back to review
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "Payment reconciled with the bank."})
    assert t.status == "Resolved" and t.resolved_at is not None
    page = student.get(f"/tickets/{ref}").data
    assert b"Resolved" in page and b"Payment reconciled with the bank." in page and b"TXN778899" in page
    events = [(e.event_type, e.to_status) for e in TicketEvent.query.filter_by(ticket_id=t.id).order_by(TicketEvent.id)]
    assert events == [("created", "Open"), ("status_changed", "Under Review"), ("status_changed", "Awaiting Student"),
                      ("student_reply", "Under Review"), ("status_changed", "Resolved")]
    acts = [a.action for a in AuditLog.query.order_by(AuditLog.id)]
    assert acts.count("update_ticket") == 3 and "ticket_reply" in acts
    assert b"Payment reconciled" in student.get("/tickets").data


def test_admin_can_reject_with_reason_and_reason_is_required(app, student, admin):
    ref = ref_of(create(student))
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Rejected", "note": ""})
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    assert t.status == "Open"                                                        # no reason, no change
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Rejected", "note": "This is outside the support scope."})
    assert t.status == "Rejected" and b"outside the support scope" in student.get(f"/tickets/{ref}").data


def test_invalid_transitions_and_reopen(app, student, admin):
    ref = ref_of(create(student))
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Closed", "note": "done"})
    assert t.status == "Closed"
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "x"})
    assert t.status == "Closed"                                                       # closed cannot jump to resolved
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Under Review"})
    assert t.status == "Under Review"                                                 # reopen allowed


def test_student_cannot_see_or_change_another_students_ticket(app, student, other_student, admin):
    ref = ref_of(create(student))
    assert other_student.get(f"/tickets/{ref}").status_code == 404
    assert other_student.post(f"/tickets/{ref}/reply", data={"message": "hello there"}).status_code == 404
    assert ref.encode() not in other_student.get("/tickets").data
    assert other_student.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "x"}).status_code == 403
    assert other_student.post(f"/api/tickets/{ref}/status", json={"status": "Resolved", "note": "x"}).status_code == 403
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    assert t.status == "Open" and TicketEvent.query.filter_by(ticket_id=t.id).count() == 1
    assert AuditLog.query.filter_by(action="view_ticket", decision="denied").count() >= 1


def test_student_cannot_reply_to_closed_request(app, student, admin):
    ref = ref_of(create(student))
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "Done."})
    student.post(f"/tickets/{ref}/reply", data={"message": "one more thing please"})
    assert TicketEvent.query.filter_by(event_type="student_reply").count() == 0


def test_admin_ajax_status_endpoint(app, student, admin):
    ref = ref_of(create(student))
    r = admin.post(f"/api/tickets/{ref}/status", json={"status": "Under Review"})
    assert r.status_code == 200 and r.get_json()["status"] == "Under Review"
    assert admin.post(f"/api/tickets/{ref}/status", json={"status": "Open"}).status_code == 400
    assert admin.post("/api/tickets/TKT-9999/status", json={"status": "Under Review"}).status_code == 404


def test_assistant_created_request_appears_for_student_and_admin(app, student, admin, student_a):
    r = handle_chat(student_a, "Create a support request for my payment issue.")
    ref = re.search(r"TKT-\d+", r["reply"]).group(0)
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    assert t.source == "assistant" and t.status == "Open"
    assert ref.encode() in student.get("/tickets").data and b"AI Assistant" in student.get(f"/tickets/{ref}").data
    assert ref.encode() in admin.get("/admin/tickets").data and b"via assistant" in admin.get("/admin/tickets").data
    assert ref.encode() in admin.get("/admin/").data
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "Handled by finance."})
    r2 = handle_chat(student_a, "Show my support requests")
    assert ref in r2["reply"]
    last = [t for t in tickets.list_tickets(student_a)][0]
    assert last["status"] == "Resolved" and last["last_response"] == "Handled by finance."


def test_counts_update_on_dashboards(app, student, admin):
    def student_open():
        return int(re.search(rb'Open support requests</div><div class="value num">(\d+)', student.get("/dashboard").data).group(1))

    def admin_open():
        return int(re.search(rb'Open support requests</div><div class="value num">(\d+)', admin.get("/admin/").data).group(1))
    s0, a0 = student_open(), admin_open()
    ref = ref_of(create(student))
    assert (student_open(), admin_open()) == (s0 + 1, a0 + 1)
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "Fixed."})
    assert (student_open(), admin_open()) == (s0, a0)


def test_search_and_filters_for_admin(app, student, admin):
    ref = ref_of(create(student))
    admin.post(f"/admin/tickets/{ref}/update", data={"status": "Resolved", "note": "ok"})
    assert ref.encode() in admin.get("/admin/tickets?status=Resolved").data and ref.encode() not in admin.get("/admin/tickets?status=active").data
    assert ref.encode() in admin.get("/admin/tickets?category=payment").data and ref.encode() not in admin.get("/admin/tickets?category=academic").data
    assert ref.encode() in admin.get("/admin/tickets?q=portal").data and ref.encode() not in admin.get("/admin/tickets?q=zzzz").data


def test_contact_change_request_links_ticket_and_approval(app, student, admin):
    r = student.post("/tickets", data={"category": "contact_change", "field": "email", "new_value": "aarav.new@example.com"})
    ref = ref_of(r)
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    a = ApprovalRequest.query.one()
    assert a.ticket_id == t.id and a.status == "pending" and StudentProfile.query.filter_by(student_id="S2026001").one().email.startswith("aarav.mehta")
    assert b"APR-0001" in student.get(f"/tickets/{ref}").data and b"APR-0001" in admin.get(f"/admin/tickets/{ref}").data
    admin.post(f"/admin/approvals/{a.id}/decide", data={"decision": "approve", "note": "verified"})
    assert StudentProfile.query.filter_by(student_id="S2026001").one().email == "aarav.new@example.com"
    assert t.status == "Resolved" and a.execution_result.startswith("Applied")
    assert SupportTicket.query.filter_by(student_id="S2026001").count() == 2             # one ticket, not duplicated


def test_rejected_linked_request_closes_ticket_without_change(app, student, admin):
    ref = ref_of(student.post("/tickets", data={"category": "contact_change", "field": "phone", "new_value": "+91 98765 43210"}))
    a = ApprovalRequest.query.one()
    admin.post(f"/admin/approvals/{a.id}/decide", data={"decision": "reject", "note": "Number could not be verified."})
    t = SupportTicket.query.filter_by(ticket_ref=ref).one()
    assert t.status == "Rejected" and "could not be verified" in t.last_response
    assert StudentProfile.query.filter_by(student_id="S2026001").one().phone == "+91 90000 10001" and a.executed_at is None


def test_invalid_contact_change_value_is_refused(app, student):
    student.post("/tickets", data={"category": "contact_change", "field": "email", "new_value": "nope"})
    assert ApprovalRequest.query.count() == 0
