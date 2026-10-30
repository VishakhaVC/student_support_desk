import json

from app.models import StudentProfile, SupportTicket
from app.tools.registry import ToolContext, ToolRegistry


def ctx(app, user):
    return ToolContext(user=user, config=app.config, retriever=app.extensions["retriever"], trace_id="t")


def run(app, user, name, args):
    return ToolRegistry().execute(name, json.dumps(args), ctx(app, user))


def test_fee_tool_returns_only_signed_in_students_data(app, student_a, student_b):
    assert run(app, student_a, "get_my_fee_status", {})["result"]["fee"]["outstanding"] == 150000
    assert run(app, student_b, "get_my_fee_status", {})["result"]["fee"]["outstanding"] == 0


def test_tool_rejects_student_id_argument(app, student_a):
    o = run(app, student_a, "get_my_fee_status", {"student_id": "S2026002"})
    assert o["status"] == "denied" and o["error_code"] == "identity_override_blocked"


def test_scholarship_tool(app, student_a):
    assert run(app, student_a, "get_my_scholarship_status", {})["result"]["scholarship"]["name"] == "Merit Scholarship"


def test_ticket_validation_rejects_bad_category_and_short_text(app, student_a):
    assert run(app, student_a, "create_support_ticket", {"category": "hacking", "subject": "Hello there", "description": "long enough text"})["error_code"] == "invalid_arguments"
    assert run(app, student_a, "create_support_ticket", {"category": "payment", "subject": "x", "description": "y"})["error_code"] == "invalid_arguments"


def test_ticket_creation_is_idempotent(app, student_a):
    args = {"category": "payment", "subject": "Portal failed", "description": "The payment portal failed twice"}
    first, second = run(app, student_a, "create_support_ticket", args), run(app, student_a, "create_support_ticket", args)
    assert first["result"]["ticket"]["duplicate"] is False and second["result"]["ticket"]["duplicate"] is True
    assert first["result"]["ticket"]["ticket_ref"] == second["result"]["ticket"]["ticket_ref"]
    assert SupportTicket.query.filter_by(student_id="S2026001").count() == 2   # 1 seeded + 1 new


def test_ticket_listing_is_scoped_to_owner(app, student_a, student_b):
    refs_a = {t["ticket_ref"] for t in run(app, student_a, "get_my_support_tickets", {})["result"]["tickets"]}
    refs_b = {t["ticket_ref"] for t in run(app, student_b, "get_my_support_tickets", {})["result"]["tickets"]}
    assert refs_a == {"TKT-0001"} and refs_b == {"TKT-0002"}


def test_policy_tool_records_evidence(app, student_a):
    c = ctx(app, student_a)
    out = ToolRegistry().execute("search_student_policy", json.dumps({"query": "payment portal failed next working day"}), c)
    assert out["result"]["found"] and c.evidence[0]["chunk_id"] == "HB-2.2"


def test_profile_change_creates_pending_request_and_leaves_record_untouched(app, student_a):
    o = run(app, student_a, "request_profile_change", {"field": "email", "new_value": "new.aarav@example.com"})
    assert o["result"]["status"] == "pending_approval"
    assert StudentProfile.query.filter_by(student_id="S2026001").first().email == "aarav.mehta@students.example.edu"


def test_profile_change_validates_format(app, student_a):
    assert run(app, student_a, "request_profile_change", {"field": "email", "new_value": "not-an-email"})["error_code"] == "invalid_arguments"
    assert run(app, student_a, "request_profile_change", {"field": "phone", "new_value": "abc"})["status"] == "error"
    assert StudentProfile.query.filter_by(student_id="S2026001").first().email == "aarav.mehta@students.example.edu"


def test_failure_injection_returns_structured_error(app, student_a):
    app.config["INJECT_TOOL_FAILURES"] = {"get_my_fee_status"}
    o = run(app, student_a, "get_my_fee_status", {})
    assert o["status"] == "error" and o["error_code"] == "tool_failure"
