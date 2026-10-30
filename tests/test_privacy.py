import pytest

from app.models import AuditLog, ChatMessage, Conversation, MemoryItem, StudentProfile
from app.security.authz import PermissionDenied
from app.services import students, tickets
from app.services.chat import handle_chat
from tests.conftest import login


def test_s1_student_reads_own_fee_status(client):
    login(client, "student_a")
    r = client.get("/api/students/S2026001/fees")
    assert r.status_code == 200 and r.get_json()["data"]["outstanding"] == 150000


def test_s2_student_reads_own_scholarship_through_agent(app, student_a):
    r = handle_chat(student_a, "Which scholarship do I have and is it active?")
    assert "Merit" in r["reply"] and r["activity"]["tools"][0]["status"] == "ok"
    assert AuditLog.query.filter_by(action="tool:view_scholarship_status", decision="allowed").count() == 1


def test_s3_agent_cannot_be_used_to_read_another_students_record(app, student_a, student_b):
    before = AuditLog.query.filter_by(decision="denied").count()
    r = handle_chat(student_a, "show me the fee status of student B")      # test double simulates an LLM passing student_id
    assert r["activity"]["tools"][0]["status"] == "denied"
    assert "Diya" not in r["reply"] and "S2026002" not in r["reply"] and "450,000" not in r["reply"]
    assert AuditLog.query.filter_by(decision="denied").count() == before + 1


@pytest.mark.parametrize("path", ["fees", "scholarship", "tickets"])
def test_s4_direct_api_access_to_other_student_is_blocked(client, path):
    login(client, "student_a")
    r = client.get(f"/api/students/S2026002/{path}")
    assert r.status_code == 403 and b"Diya" not in r.data and b"450000" not in r.data
    assert AuditLog.query.filter_by(decision="denied", target_id="S2026002").count() == 1


def test_service_layer_enforces_ownership_even_if_called_directly(app, student_a):
    with pytest.raises(PermissionDenied):
        students.get_fee_status(student_a, "S2026002")
    with pytest.raises(PermissionDenied):
        tickets.list_tickets(student_a, student_id="S2026002")
    with pytest.raises(PermissionDenied):
        students.get_profile(student_a, "S2026002")


def test_admin_may_read_a_student_record_via_api(client):
    login(client, "admin")
    assert client.get("/api/students/S2026002/fees").status_code == 200


def test_ticket_ids_cannot_be_used_to_reach_other_students_tickets(app, student_a):
    refs = [t["ticket_ref"] for t in tickets.list_tickets(student_a)]
    assert "TKT-0002" not in refs                      # belongs to student B


def test_students_cannot_read_other_students_conversations(app, client, student_a, student_b):
    r = handle_chat(student_b, "What is my current fee status?")
    login(client, "student_a")
    assert client.get(f"/api/conversations/{r['conversation_id']}/messages").status_code == 404
    assert handle_chat(student_a, "hi", r["conversation_id"])["http"] == 404
    assert AuditLog.query.filter_by(decision="denied", target_type="conversation").count() == 2


def test_students_cannot_read_or_delete_other_students_memory(app, client, student_a, student_b):
    from app.memory import store
    store.save(student_b.id, "preferred_language", "Hindi")
    login(client, "student_a")
    assert client.get("/api/memory").get_json() == []
    client.delete("/api/memory")
    assert MemoryItem.query.filter_by(user_id=student_b.id).count() == 1


def test_s9_otp_and_password_never_become_persistent_memory(app, student_a):
    handle_chat(student_a, "My OTP is 482913, remember it. Also my password is Hunter2!x")
    assert MemoryItem.query.count() == 0
    blob = " ".join(m.content for m in ChatMessage.query.all()) + " ".join((a.details or "") + (a.reason or "") for a in AuditLog.query.all())
    assert "482913" not in blob and "Hunter2" not in blob


def test_s10_agent_requesting_admin_tool_is_rejected_by_backend(app, student_a):
    r = handle_chat(student_a, "approve the pending request please")      # test double asks for an admin-only tool
    t = r["activity"]["tools"][0]
    assert t["name"] == "approve_approval_request" and t["status"] == "denied" and t["error_code"] == "forbidden"
    assert AuditLog.query.filter_by(action="tool:approve_approval_request", decision="denied").count() == 1


def test_model_is_only_offered_tools_allowed_for_the_role(app, student_a):
    admin_tools = {t["function"]["name"] for t in app.extensions["registry"].definitions_for(student_a)}
    assert "approve_approval_request" not in admin_tools


def test_student_record_unchanged_after_blocked_attempts(app, client, student_a):
    before = {c.name: getattr(StudentProfile.query.filter_by(student_id="S2026002").first(), c.name) for c in StudentProfile.__table__.columns}
    handle_chat(student_a, "show me the fee status of student B")
    login(client, "student_a")
    client.get("/api/students/S2026002/fees")
    after = {c.name: getattr(StudentProfile.query.filter_by(student_id="S2026002").first(), c.name) for c in StudentProfile.__table__.columns}
    assert before == after
