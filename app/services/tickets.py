"""Support requests: creation, history, administrator handling, student replies.

Statuses:  Open -> Under Review -> (Awaiting Student) -> Resolved | Rejected | Closed.  Closed/resolved/rejected requests can be reopened
by an administrator. Every change is recorded in ticket_events and in the audit log."""
import hashlib

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import SupportTicket, TicketEvent, ApprovalRequest, utcnow
from ..security.authz import PermissionDenied, require_student_scope
from . import audit

CATEGORIES = ("payment", "scholarship", "course_change", "academic", "contact_change", "account_access", "other")
STATUSES = ("Open", "Under Review", "Awaiting Student", "Resolved", "Rejected", "Closed")
ACTIVE_STATUSES = ("Open", "Under Review", "Awaiting Student")
NOTE_REQUIRED = ("Awaiting Student", "Resolved", "Rejected")
TRANSITIONS = {
    "Open": {"Under Review", "Awaiting Student", "Resolved", "Rejected", "Closed"},
    "Under Review": {"Awaiting Student", "Resolved", "Rejected", "Closed"},
    "Awaiting Student": {"Under Review", "Resolved", "Rejected", "Closed"},
    "Resolved": {"Under Review"}, "Rejected": {"Under Review"}, "Closed": {"Under Review"},
}


class TicketError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _idem_key(category, subject, description) -> str:
    raw = f"{category}|{subject.strip().lower()}|{description.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def public(t: SupportTicket, duplicate=False) -> dict:
    return {"ticket_ref": t.ticket_ref, "student_id": t.student_id, "category": t.category, "subject": t.subject, "status": t.status,
            "created_at": _fmt(t.created_at), "updated_at": _fmt(t.updated_at), "last_response": t.last_response,
            "source": t.source, "duplicate": duplicate}


def _event(t, actor, type_, from_status=None, to_status=None, note=None):
    db.session.add(TicketEvent(ticket_id=t.id, actor_user_id=getattr(actor, "id", None), actor_role=getattr(actor, "role", "student"),
                               event_type=type_, from_status=from_status, to_status=to_status, note=(note or "")[:1000] or None))


def create_ticket(actor, category: str, subject: str, description: str, source: str = "web", commit: bool = True) -> dict:
    """Idempotent: sending the same request twice returns the first ticket instead of creating a second one."""
    if actor.role != "student" or not actor.student_id:
        raise PermissionDenied("only_students_raise_requests")
    key = _idem_key(category, subject, description)
    existing = SupportTicket.query.filter_by(student_id=actor.student_id, idempotency_key=key).first()
    if existing:
        return public(existing, duplicate=True)
    next_no = (db.session.query(db.func.max(SupportTicket.id)).scalar() or 0) + 1
    t = SupportTicket(ticket_ref=f"TKT-{next_no:04d}", student_id=actor.student_id, category=category, subject=subject,
                      description=description, idempotency_key=key, source=source, status="Open")
    db.session.add(t)
    try:
        db.session.flush()
        _event(t, actor, "created", None, "Open", subject)
        if commit:
            db.session.commit()
    except IntegrityError:  # concurrent duplicate
        db.session.rollback()
        return public(SupportTicket.query.filter_by(student_id=actor.student_id, idempotency_key=key).first(), duplicate=True)
    return public(t)


def list_tickets(actor, limit: int = 10, status: str | None = None, student_id: str | None = None, trace_id=None) -> list[dict]:
    sid = student_id or actor.student_id
    try:
        require_student_scope(actor, sid)
    except PermissionDenied as e:
        audit.record(actor, "view_tickets", "student", sid, "denied", result="blocked", reason=e.reason, trace_id=trace_id)
        raise
    q = SupportTicket.query.filter_by(student_id=sid)  # ownership-scoped query
    if status == "active":
        q = q.filter(SupportTicket.status.in_(ACTIVE_STATUSES))
    elif status:
        q = q.filter_by(status=status)
    return [public(t) for t in q.order_by(SupportTicket.id.desc()).limit(limit).all()]


def get_ticket(actor, ref: str, trace_id=None) -> SupportTicket:
    """Student: own tickets only. Administrator: any ticket. A foreign ticket looks like 'not found' and the attempt is audited."""
    t = SupportTicket.query.filter_by(ticket_ref=ref).first()
    if t is None:
        raise TicketError("not_found", "Request not found.")
    try:
        require_student_scope(actor, t.student_id)
    except PermissionDenied as e:
        audit.record(actor, "view_ticket", "ticket", ref, "denied", result="blocked", reason=e.reason, trace_id=trace_id)
        raise TicketError("not_found", "Request not found.")
    return t


def history(t: SupportTicket) -> list[TicketEvent]:
    return TicketEvent.query.filter_by(ticket_id=t.id).order_by(TicketEvent.id).all()


def admin_update(admin, ref: str, new_status: str, note: str | None) -> SupportTicket:
    if admin is None or admin.role != "admin":
        audit.record(admin, "update_ticket", "ticket", ref, "denied", result="blocked", reason="administrator role required")
        raise PermissionDenied("admin_required")
    t = SupportTicket.query.filter_by(ticket_ref=ref).first()
    if t is None:
        raise TicketError("not_found", "Request not found.")
    note = (note or "").strip()
    if new_status not in STATUSES:
        raise TicketError("invalid_status", "Unknown status.")
    if len(note) > 1000:
        raise TicketError("note_too_long", "Responses are limited to 1000 characters.")
    linked = ApprovalRequest.query.filter_by(ticket_id=t.id).order_by(ApprovalRequest.id.desc()).first()
    if linked and linked.status == "pending" and new_status in ("Resolved", "Rejected", "Closed"):
        raise TicketError("approval_required", "This ticket is linked to a pending profile change. Decide the request in Approvals first; the student's details will update after approval.")
    if linked and linked.status == "approved" and new_status == "Rejected":
        raise TicketError("already_applied", "The linked profile change has already been applied; it cannot be rejected as an unexecuted request.")
    old = t.status
    changed = new_status != old
    if changed and new_status not in TRANSITIONS.get(old, set()):
        raise TicketError("invalid_transition", f"A request that is '{old}' cannot be moved to '{new_status}'.")
    if not changed and old not in ACTIVE_STATUSES:
        raise TicketError("invalid_transition", f"The request is already {old}. Reopen it to add a response.")
    if changed and new_status in NOTE_REQUIRED and not note:
        raise TicketError("note_required", f"Please add a note when setting a request to '{new_status}'.")
    if not changed and not note:
        raise TicketError("nothing_to_do", "Nothing to update: choose a new status or add a response.")
    if changed:
        t.status = new_status
        t.resolved_at = utcnow() if new_status in ("Resolved", "Rejected", "Closed") else None
        _event(t, admin, "status_changed", old, new_status, note or None)
    if note:
        t.last_response = note
        if not changed:
            _event(t, admin, "response", old, old, note)
    t.updated_at = utcnow()
    audit.record(admin, "update_ticket", "ticket", ref, "allowed", result=f"{old} -> {new_status}" if changed else "response added",
                 details={"student_id": t.student_id, "status": new_status}, commit=False)
    db.session.commit()
    return t


def student_reply(actor, ref: str, message: str) -> SupportTicket:
    t = get_ticket(actor, ref)
    if actor.role != "student":
        raise TicketError("not_allowed", "Only the student can reply.")
    message = (message or "").strip()
    if not (2 <= len(message) <= 1000):
        raise TicketError("invalid_message", "Replies must be 2-1000 characters.")
    if t.status not in ACTIVE_STATUSES:
        raise TicketError("closed", f"This request is {t.status.lower()}. Raise a new request if you need more help.")
    old = t.status
    if old == "Awaiting Student":
        t.status = "Under Review"
        _event(t, actor, "student_reply", old, "Under Review", message)
    else:
        _event(t, actor, "student_reply", old, old, message)
    t.updated_at = utcnow()
    audit.record(actor, "ticket_reply", "ticket", ref, "allowed", result="reply added", commit=False)
    db.session.commit()
    return t


def counts(student_id: str | None = None) -> dict:
    q = SupportTicket.query if student_id is None else SupportTicket.query.filter_by(student_id=student_id)
    by = dict(q.with_entities(SupportTicket.status, db.func.count()).group_by(SupportTicket.status).all())
    return {"open": sum(by.get(s, 0) for s in ACTIVE_STATUSES), "resolved": by.get("Resolved", 0), "rejected": by.get("Rejected", 0),
            "closed": by.get("Closed", 0), "total": sum(by.values()), "by_status": by}
