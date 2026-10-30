"""Student record services. Identity comes from the server-side signed-in user (`actor`), never from the LLM.
Every function enforces ownership in code; denials are audited."""
from ..models import FeeRecord, ScholarshipRecord, StudentProfile
from ..security.authz import PermissionDenied, require_student_scope
from . import audit


def _scope(actor, student_id, what, trace_id):
    sid = student_id or actor.student_id
    try:
        require_student_scope(actor, sid)
    except PermissionDenied as e:
        audit.record(actor, what, "student", sid, "denied", result="blocked", reason=e.reason, trace_id=trace_id)
        raise
    return sid


def get_profile(actor, student_id=None, trace_id=None):
    sid = _scope(actor, student_id, "view_profile", trace_id)
    return StudentProfile.query.filter_by(student_id=sid).first()


def get_fee_status(actor, student_id=None, trace_id=None):
    sid = _scope(actor, student_id, "view_fee_status", trace_id)
    rec = FeeRecord.query.filter_by(student_id=sid).order_by(FeeRecord.id.desc()).first()   # ownership-scoped query
    if not rec:
        return None
    return {"term": rec.term, "total_fee": rec.total_fee, "paid": rec.paid, "outstanding": rec.outstanding,
            "due_date": rec.due_date, "status": rec.status, "currency": "INR"}


def get_scholarship_status(actor, student_id=None, trace_id=None):
    sid = _scope(actor, student_id, "view_scholarship_status", trace_id)
    rec = ScholarshipRecord.query.filter_by(student_id=sid).order_by(ScholarshipRecord.id.desc()).first()
    if not rec:
        return None
    return {"name": rec.name, "percent": rec.percent, "status": rec.status, "condition": rec.condition,
            "next_review": rec.next_review}
