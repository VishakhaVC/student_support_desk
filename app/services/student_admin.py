"""Administrator student management. Every function requires the administrator role, validates input, and writes an audit record.
Removal is a soft delete (account disabled, record kept) so audit history and references stay intact; no hard delete exists."""
from werkzeug.security import generate_password_hash

from ..extensions import db
from ..models import ApprovalRequest, FeeRecord, StudentProfile, SupportTicket, User
from ..security.authz import PermissionDenied
from . import audit
from .validators import (EMAIL, PHONE, STUDENT_ID, TRIMESTERS, USERNAME, ValidationError, generate_password, password_problems)

STATUSES = ("active", "inactive", "removed")


def _admin_only(actor, action, target=None):
    if actor is None or getattr(actor, "role", None) != "admin":
        audit.record(actor, action, "student", target, "denied", result="blocked", reason="administrator role required")
        raise PermissionDenied("admin_required")


def _clean(data: dict, key: str, maxlen: int) -> str:
    return (data.get(key) or "").strip()[:maxlen]


def _validate_profile(data: dict, errors: dict) -> dict:
    out = {"full_name": _clean(data, "full_name", 80), "programme": _clean(data, "programme", 120), "trimester": _clean(data, "trimester", 20),
           "email": _clean(data, "email", 120).lower(), "phone": _clean(data, "phone", 30)}
    if len(out["full_name"]) < 2:
        errors["full_name"] = "Enter the student's full name."
    if len(out["programme"]) < 2:
        errors["programme"] = "Enter the programme."
    if out["trimester"] not in TRIMESTERS:
        errors["trimester"] = "Choose a trimester."
    if not EMAIL.match(out["email"]):
        errors["email"] = "Enter a valid email address."
    if not PHONE.match(out["phone"]):
        errors["phone"] = "Enter a valid phone number (8-15 digits, optional +)."
    try:
        cgpa = float(str(data.get("cgpa", "")).strip())
        if not 0.0 <= cgpa <= 4.0:
            raise ValueError
        out["cgpa"] = round(cgpa, 2)
    except ValueError:
        errors["cgpa"] = "CGPA must be a number between 0 and 4.0."
    return out


def list_students(q: str | None = None, status: str | None = None, programme: str | None = None) -> list[StudentProfile]:
    query = StudentProfile.query
    if status in STATUSES:
        query = query.filter(StudentProfile.status == status)
    else:
        query = query.filter(StudentProfile.status != "removed")      # removed students are hidden unless asked for
    if programme:
        query = query.filter(StudentProfile.programme == programme)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(db.or_(StudentProfile.full_name.like(like), StudentProfile.student_id.like(like),
                                    StudentProfile.email.like(like), StudentProfile.programme.like(like)))
    return query.order_by(StudentProfile.student_id).all()


def programmes() -> list[str]:
    return [p for (p,) in db.session.query(StudentProfile.programme).distinct().order_by(StudentProfile.programme).all()]


def account_for(student_id: str) -> User | None:
    return User.query.filter_by(student_id=student_id, role="student").first()


def overview(student_id: str) -> dict:
    p = StudentProfile.query.filter_by(student_id=student_id).first()
    if p is None:
        return {}
    fee = FeeRecord.query.filter_by(student_id=student_id).order_by(FeeRecord.id.desc()).first()
    from ..models import ScholarshipRecord
    sch = ScholarshipRecord.query.filter_by(student_id=student_id).order_by(ScholarshipRecord.id.desc()).first()
    return {"profile": p, "account": account_for(student_id), "fee": fee, "scholarship": sch,
            "tickets": SupportTicket.query.filter_by(student_id=student_id).order_by(SupportTicket.id.desc()).all(),
            "approvals": ApprovalRequest.query.filter_by(student_id=student_id).order_by(ApprovalRequest.id.desc()).all()}


def create_student(admin, data: dict) -> tuple[StudentProfile, str | None]:
    """Returns (profile, generated_password_or_None). A generated password is shown to the administrator once and never stored in clear."""
    _admin_only(admin, "create_student")
    errors: dict[str, str] = {}
    clean = _validate_profile(data, errors)
    sid, username = _clean(data, "student_id", 20), _clean(data, "username", 30).lower()
    if not STUDENT_ID.match(sid):
        errors["student_id"] = "Use 4-20 letters, digits or hyphens."
    elif StudentProfile.query.filter_by(student_id=sid).first():
        errors["student_id"] = "That student ID already exists."
    if not USERNAME.match(username):
        errors["username"] = "Use 3-30 lowercase letters, digits, dots or underscores."
    elif User.query.filter_by(username=username).first():
        errors["username"] = "That username is already taken."
    if clean.get("email") and "email" not in errors and StudentProfile.query.filter_by(email=clean["email"]).first():
        errors["email"] = "Another student already uses that email."
    status = data.get("status", "active")
    if status not in ("active", "inactive"):
        errors["status"] = "Choose active or inactive."
    password, generated = (data.get("password") or ""), None
    try:
        fee_total = int(str(data.get("total_fee") or "450000").strip())
        if not 0 <= fee_total <= 10_000_000:
            raise ValueError
    except ValueError:
        errors["total_fee"] = "Enter the trimester fee as a whole number."
        fee_total = 0
    due_date = _clean(data, "due_date", 12) or "2026-12-15"
    if not password:
        password = generated = generate_password()
    else:
        problems = password_problems(password)
        if problems:
            errors["password"] = "Password needs " + ", ".join(problems) + "."
    if errors:
        audit.record(admin, "create_student", "student", sid or None, "denied", result="validation_failed", reason=", ".join(sorted(errors)))
        raise ValidationError(errors)
    profile = StudentProfile(student_id=sid, status=status, **clean)
    db.session.add(profile)
    db.session.flush()
    db.session.add(User(username=username, password_hash=generate_password_hash(password), role="student", display_name=clean["full_name"],
                        student_id=sid, active=(status == "active")))
    db.session.add(FeeRecord(student_id=sid, term=clean["trimester"], total_fee=fee_total, paid=0, due_date=due_date,
                             status="Paid" if fee_total == 0 else "Unpaid"))
    audit.record(admin, "create_student", "student", sid, "allowed", result="created",
                 details={"username": username, "programme": clean["programme"], "status": status}, commit=False)
    db.session.commit()
    return profile, generated


def update_student(admin, student_id: str, data: dict) -> StudentProfile:
    _admin_only(admin, "update_student", student_id)
    p = StudentProfile.query.filter_by(student_id=student_id).first()
    if p is None:
        raise ValidationError({"student_id": "Student not found."})
    errors: dict[str, str] = {}
    clean = _validate_profile(data, errors)
    if "email" not in errors:
        other = StudentProfile.query.filter(StudentProfile.email == clean["email"], StudentProfile.student_id != student_id).first()
        if other:
            errors["email"] = "Another student already uses that email."
    if errors:
        audit.record(admin, "update_student", "student", student_id, "denied", result="validation_failed", reason=", ".join(sorted(errors)))
        raise ValidationError(errors)
    changed = [k for k, v in clean.items() if getattr(p, k) != v]
    for k, v in clean.items():
        setattr(p, k, v)
    acct = account_for(student_id)
    if acct:
        acct.display_name = clean["full_name"]
    audit.record(admin, "update_student", "student", student_id, "allowed", result="updated", details={"fields": ",".join(changed) or "none"}, commit=False)
    db.session.commit()
    return p


def set_status(admin, student_id: str, status: str) -> StudentProfile:
    """active / inactive / removed. The account is disabled at once; an open session is ended on its next request."""
    _admin_only(admin, "set_student_status", student_id)
    if status not in STATUSES:
        raise ValidationError({"status": "Unknown status."})
    p = StudentProfile.query.filter_by(student_id=student_id).first()
    if p is None:
        raise ValidationError({"student_id": "Student not found."})
    old = p.status
    p.status = status
    acct = account_for(student_id)
    if acct:
        acct.active = status == "active"
    action = {"active": "reactivate_student", "inactive": "deactivate_student", "removed": "remove_student"}[status]
    audit.record(admin, action, "student", student_id, "allowed", result=f"{old} -> {status}", commit=False)
    db.session.commit()
    return p


def reset_password(admin, student_id: str, new_password: str | None = None) -> str:
    """Sets a new password (generated when none is given) and returns it for one-time display. It is never logged or audited."""
    _admin_only(admin, "reset_password", student_id)
    acct = account_for(student_id)
    if acct is None:
        raise ValidationError({"student_id": "This student has no account."})
    if new_password:
        problems = password_problems(new_password)
        if problems:
            raise ValidationError({"password": "Password needs " + ", ".join(problems) + "."})
    pw = new_password or generate_password()
    acct.password_hash = generate_password_hash(pw)
    audit.record(admin, "reset_password", "student", student_id, "allowed", result="password reset", commit=False)
    db.session.commit()
    return pw


def counts() -> dict:
    by = dict(db.session.query(StudentProfile.status, db.func.count()).group_by(StudentProfile.status).all())
    return {"active": by.get("active", 0), "inactive": by.get("inactive", 0), "removed": by.get("removed", 0)}
