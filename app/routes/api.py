from flask import Blueprint, abort, jsonify, request
from flask_login import current_user

from ..memory import store as memory
from ..extensions import db
from ..models import AgentRun, ApprovalRequest, ChatMessage, Conversation
from ..security.authz import PermissionDenied, role_required
from ..services import approvals, audit, students, tickets
from ..services.chat import handle_chat

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/chat", methods=["POST"])
@role_required("student", "admin")
def chat():
    data = request.get_json(silent=True) or {}
    result = handle_chat(current_user, data.get("message", ""), data.get("conversation_id"))
    http = result.pop("http", 200)
    return jsonify(result), http


@bp.route("/conversations")
@role_required("student", "admin")
def conversations():
    rows = Conversation.query.filter_by(user_id=current_user.id).order_by(Conversation.updated_at.desc()).limit(15).all()
    return jsonify([{"id": c.id, "title": c.title} for c in rows])


@bp.route("/conversations/<int:cid>/messages")
@role_required("student", "admin")
def conversation_messages(cid):
    conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first()
    if conv is None:
        if db_exists(cid):
            audit.record(current_user, "view_conversation", "conversation", cid, "denied", result="blocked", reason="not the owner")
        abort(404)
    rows = ChatMessage.query.filter_by(conversation_id=conv.id).order_by(ChatMessage.id).all()
    return jsonify([{"role": m.role, "content": m.content, "time": m.created_at.strftime("%H:%M"), "date": m.created_at.strftime("%d %b %Y")} for m in rows])


@bp.route("/conversations/<int:cid>/metrics")
@role_required("student", "admin")
def conversation_metrics(cid):
    # Bind conversation and agent run to authenticated user; never trust the requested id alone.
    conv = Conversation.query.filter_by(id=cid, user_id=current_user.id).first_or_404()
    run = (AgentRun.query.filter_by(conversation_id=conv.id, user_id=current_user.id)
           .order_by(AgentRun.id.desc()).first())
    if run is None:
        return jsonify({"model": None, "latency_ms": None, "iterations": None, "tool_call_count": None,
                        "tokens": {"input_tokens": None, "output_tokens": None, "total_tokens": None}})
    return jsonify({"model": run.model, "latency_ms": run.latency_ms, "iterations": run.iterations,
                    "tool_call_count": run.tool_call_count,
                    "tokens": {"input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
                               "total_tokens": run.total_tokens}})


def _delete_conversations(user, only_id=None):
    q = Conversation.query.filter_by(user_id=user.id)
    if only_id:
        q = q.filter_by(id=only_id)
    ids = [c.id for c in q.all()]
    if ids:
        db.session.query(AgentRun).filter(AgentRun.conversation_id.in_(ids)).update({"conversation_id": None}, synchronize_session=False)
        ChatMessage.query.filter(ChatMessage.conversation_id.in_(ids)).delete(synchronize_session=False)
        Conversation.query.filter(Conversation.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
    audit.record(user, "delete_conversation", "conversation", only_id or "*", "allowed", result=f"deleted={len(ids)}")
    return len(ids)


@bp.route("/conversations/<int:cid>", methods=["DELETE"])
@role_required("student", "admin")
def conversation_delete(cid):
    n = _delete_conversations(current_user, cid)
    return (jsonify({"deleted": n}), 200) if n else (jsonify({"ok": False, "error": "not_found"}), 404)


@bp.route("/conversations", methods=["DELETE"])
@role_required("student", "admin")
def conversations_clear():
    return jsonify({"deleted": _delete_conversations(current_user)})


def db_exists(cid):
    return Conversation.query.filter_by(id=cid).first() is not None


@bp.route("/memory", methods=["GET"])
@role_required("student")
def memory_list():
    return jsonify([{"key": m.key, "value": m.value} for m in memory.list_items(current_user.id)])


@bp.route("/memory/<key>", methods=["DELETE"])
@role_required("student")
def memory_delete_one(key):
    n = memory.delete(current_user.id, key)
    audit.record(current_user, "delete_memory", "memory", key, "allowed", result=f"deleted={n}")
    return jsonify({"deleted": n})


@bp.route("/memory", methods=["DELETE"])
@role_required("student")
def memory_delete_all():
    n = memory.delete(current_user.id)
    audit.record(current_user, "delete_memory", "memory", "*", "allowed", result=f"deleted={n}")
    return jsonify({"deleted": n})


# ---- direct record access: the same ownership rule as the agent tools, enforced in the service layer ----
def _guard(fn, action, sid):
    try:
        data = fn()
    except PermissionDenied as e:
        return jsonify({"ok": False, "error": "forbidden", "message": "You may only access your own records."}), 403
    audit.record(current_user, f"api:{action}", "student", sid, "allowed", result="ok")
    return jsonify({"ok": True, "data": data})


@bp.route("/students/<student_id>/fees")
@role_required("student", "admin")
def student_fees(student_id):
    return _guard(lambda: students.get_fee_status(current_user, student_id), "view_fee_status", student_id)


@bp.route("/students/<student_id>/scholarship")
@role_required("student", "admin")
def student_scholarship(student_id):
    return _guard(lambda: students.get_scholarship_status(current_user, student_id), "view_scholarship_status", student_id)


@bp.route("/students/<student_id>/tickets")
@role_required("student", "admin")
def student_tickets(student_id):
    return _guard(lambda: tickets.list_tickets(current_user, 50, student_id=student_id), "view_tickets", student_id)


# ---- approvals ----
@bp.route("/approvals")
@role_required("student", "admin")
def approvals_list():
    rows = approvals.pending() if current_user.role == "admin" else approvals.for_student(current_user)
    return jsonify([{"id": r.id, "ref": r.approval_ref, "student_id": r.student_id, "field": r.field, "status": r.status} for r in rows])


def _decide(approval_id, approve):
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
    if not approve and current_user.role == "admin" and len(note) < 3:
        return jsonify({"ok": False, "error": "reason_required", "message": "Please give a reason when rejecting."}), 400
    try:
        req = approvals.decide(current_user, approval_id, approve, note)
    except PermissionDenied as e:
        return jsonify({"ok": False, "error": e.reason}), 403
    except approvals.ApprovalStateError as e:
        return jsonify({"ok": False, "error": str(e)}), 404 if str(e) == "not_found" else 409
    except ValueError as e:
        return jsonify({"ok": False, "error": "execution_failed", "message": str(e)}), 422
    return jsonify({"ok": True, "ref": req.approval_ref, "status": req.status, "executed": req.executed_at is not None})


@bp.route("/approvals/<int:approval_id>/approve", methods=["POST"])
@role_required("student", "admin")   # students are let in only so the service can refuse and audit the attempt
def approve(approval_id):
    return _decide(approval_id, True)


@bp.route("/approvals/<int:approval_id>/reject", methods=["POST"])
@role_required("student", "admin")
def reject(approval_id):
    return _decide(approval_id, False)


@bp.route("/tickets/<ref>/status", methods=["POST"])
@role_required("admin")
def ticket_status(ref):
    from ..services import tickets as tsvc
    data = request.get_json(silent=True) or {}
    try:
        t = tsvc.admin_update(current_user, ref, data.get("status", ""), data.get("note"))
    except tsvc.TicketError as e:
        return jsonify({"ok": False, "error": e.code, "message": e.message}), 400 if e.code != "not_found" else 404
    return jsonify({"ok": True, "ref": t.ticket_ref, "status": t.status})
