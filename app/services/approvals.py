"""Human-in-the-loop approval workflow for sensitive changes (registered email / phone).

The agent can only CREATE a pending request. Applying the change happens only in `decide()`, called by an administrator route,
which re-checks role, self-approval and request state, then flips status with an atomic compare-and-set so a request can be
executed at most once (no replay, no double execution)."""
from ..extensions import db
from ..models import ApprovalRequest, StudentProfile, SupportTicket, TicketEvent, User, utcnow
from ..security.authz import PermissionDenied
from .validators import EMAIL, PHONE
import re
from . import audit


class ApprovalStateError(Exception):
    pass


def _next_ref() -> str:
    return f"APR-{(db.session.query(db.func.max(ApprovalRequest.id)).scalar() or 0) + 1:04d}"


def request_contact_change(actor, field: str, new_value: str, reason: str | None, trace_id: str | None, ticket_id: int | None = None) -> dict:
    if actor.role != "student" or not actor.student_id:
        audit.record(actor, "request_contact_change", "student", None, "denied", reason="only students can request changes to their own record", trace_id=trace_id)
        raise PermissionDenied("only_students_request_changes")
    new_value = (new_value or "").strip()
    if field == "full_name":
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,78}", new_value):
            raise ValueError("Enter a valid full name (2–79 characters).")
    elif field == "email":
        if not EMAIL.fullmatch(new_value):
            raise ValueError("Enter a valid email address.")
    elif field == "phone":
        if not PHONE.fullmatch(new_value):
            raise ValueError("Enter a valid phone number.")
    else:
        raise ValueError("Unsupported profile field.")
    existing = ApprovalRequest.query.filter_by(student_id=actor.student_id, field=field, new_value=new_value, status="pending").first()
    if existing:
        # A student may have requested the change on their profile first and later link a support ticket.
        # Link only an unlinked pending approval; do not create a competing request.
        if ticket_id is not None:
            if existing.ticket_id is not None and existing.ticket_id != ticket_id:
                raise ValueError(f"This same change is already awaiting review as {existing.approval_ref}. Check your Approvals page.")
            ticket = db.session.get(SupportTicket, ticket_id)
            if ticket is None or ticket.student_id != actor.student_id:
                raise PermissionDenied("not_owner_of_ticket")
            if existing.ticket_id is None:
                existing.ticket_id = ticket_id
                db.session.commit()
        return {"ok": True, "status": "pending_approval", "approval_ref": existing.approval_ref, "field": field, "duplicate": True}
    req = ApprovalRequest(approval_ref=_next_ref(), requester_user_id=actor.id, student_id=actor.student_id, field=field,
                          new_value=new_value, reason=reason, trace_id=trace_id, ticket_id=ticket_id)
    db.session.add(req)
    db.session.flush()
    audit.record(actor, "request_contact_change", "approval", req.approval_ref, "pending", result="approval_required", trace_id=trace_id,
                 details={"field": field, "new_value_masked": audit.mask(new_value), "student_id": actor.student_id}, commit=False)
    db.session.commit()
    return {"ok": True, "status": "pending_approval", "approval_ref": req.approval_ref, "field": field, "duplicate": False}


def _deny(admin, req_ref, reason, trace_id):
    audit.record(admin, "decide_approval", "approval", req_ref, "denied", result="blocked", reason=reason, trace_id=trace_id)


def decide(admin, approval_id: int, approve: bool, note: str | None = None, trace_id: str | None = None) -> ApprovalRequest:
    ref = str(approval_id)
    if admin is None or admin.role != "admin":
        _deny(admin, ref, "caller is not an administrator", trace_id)
        raise PermissionDenied("admin_required")
    req = db.session.get(ApprovalRequest, approval_id)
    if req is None:
        raise ApprovalStateError("not_found")
    ref = req.approval_ref
    if req.requester_user_id == admin.id:
        _deny(admin, ref, "self-approval is not allowed", trace_id)
        raise PermissionDenied("self_approval_not_allowed")

    new_status = "approved" if approve else "rejected"
    # Atomic compare-and-set: succeeds only while the request is still pending, so a replay or a second admin click is a no-op.
    changed = (ApprovalRequest.query.filter_by(id=req.id, status="pending")
               .update({"status": new_status, "decided_by_user_id": admin.id, "decided_at": utcnow(), "decision_note": (note or "")[:200] or None},
                       synchronize_session=False))
    if changed != 1:
        db.session.rollback()
        _deny(admin, ref, "request is no longer pending (replay or already decided)", trace_id)
        raise ApprovalStateError("not_pending")
    db.session.refresh(req)

    if not approve:
        req.execution_result = "Not applied: request rejected"
        _close_linked_ticket(req, admin, "Rejected", (note or "Your change request was rejected.")[:200])
        audit.record(admin, "decide_approval", "approval", ref, "rejected", result="not_executed", trace_id=req.trace_id or trace_id,
                     details={"student_id": req.student_id, "field": req.field}, commit=False)
        db.session.commit()
        return req

    try:  # re-validate everything at execution time; do not trust data stored earlier
        requester = db.session.get(User, req.requester_user_id)
        profile = StudentProfile.query.filter_by(student_id=req.student_id).first()
        if not requester or requester.student_id != req.student_id or profile is None or req.field not in ("full_name", "email", "phone"):
            raise ValueError("request no longer valid")
        if not requester.active or profile.status != "active":
            raise ValueError("student account is not active")
        old = getattr(profile, req.field)
        if req.field == "email" and StudentProfile.query.filter(StudentProfile.student_id != req.student_id, StudentProfile.email == req.new_value).first():
            raise ValueError("Email address is already registered")
        setattr(profile, req.field, req.new_value)
        if req.field == "full_name":
            requester.display_name = req.new_value
        req.executed_at = utcnow()
        req.execution_result = f"Applied: registered {req.field.replace('_', ' ')} updated"
        _close_linked_ticket(req, admin, "Resolved", "Your change was approved and applied.")
        audit.record(admin, "decide_approval", "approval", ref, "approved", result="executed", trace_id=req.trace_id or trace_id,
                     details={"student_id": req.student_id, "field": req.field, "old_masked": audit.mask(old),
                              "new_value_masked": audit.mask(req.new_value)}, commit=False)
        db.session.commit()     # status change + data change + audit record commit together or not at all
    except Exception:
        db.session.rollback()   # request goes back to pending, student record untouched
        audit.record(admin, "decide_approval", "approval", ref, "error", result="execution_failed", trace_id=trace_id)
        raise
    return req


def _close_linked_ticket(req, admin, status, note):
    """If the approval was raised from a support request, record the outcome on that request too (same transaction)."""
    if not req.ticket_id:
        return
    t = db.session.get(SupportTicket, req.ticket_id)
    if t is None or t.status not in ("Open", "Under Review", "Awaiting Student"):
        return
    old = t.status
    t.status, t.last_response, t.updated_at, t.resolved_at = status, note, utcnow(), utcnow()
    db.session.add(TicketEvent(ticket_id=t.id, actor_user_id=admin.id, actor_role="admin", event_type="status_changed",
                               from_status=old, to_status=status, note=note))


def list_all(status: str | None = None, q: str | None = None) -> list[ApprovalRequest]:
    query = ApprovalRequest.query
    if status:
        query = query.filter_by(status=status)
    if q:
        like = f"%{q.strip()}%"
        names = db.session.query(StudentProfile.student_id).filter(StudentProfile.full_name.like(like))
        query = query.filter(db.or_(ApprovalRequest.approval_ref.like(like), ApprovalRequest.student_id.like(like),
                                    ApprovalRequest.field.like(like), ApprovalRequest.student_id.in_(names)))
    return query.order_by(ApprovalRequest.id.desc()).all()


def pending() -> list[ApprovalRequest]:
    return ApprovalRequest.query.filter_by(status="pending").order_by(ApprovalRequest.id).all()


def for_student(actor) -> list[ApprovalRequest]:
    return ApprovalRequest.query.filter_by(student_id=actor.student_id).order_by(ApprovalRequest.id.desc()).all()   # ownership-scoped
