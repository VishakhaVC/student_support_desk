"""Authorization rules enforced in normal Python code. The LLM is never consulted and never trusted for these decisions."""
from functools import wraps

from flask import abort, jsonify, request
from flask_login import current_user


class PermissionDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_student_scope(actor, student_id: str) -> str:
    """A student may touch only their own record; an administrator may read any record (and that is audited by callers)."""
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("not_authenticated")
    if actor.role == "admin":
        return "admin"
    if actor.role == "student" and actor.student_id and actor.student_id == student_id:
        return "owner"
    raise PermissionDenied("not_owner_of_record")


def role_required(*roles):
    """Route decorator: authentication first, then role. Denials are audited and answered with 403."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user.is_authenticated:
                return abort(401) if request.path.startswith("/api/") else current_user_unauthorized()
            if current_user.role not in roles:
                from ..services import audit
                audit.record(current_user, f"access:{request.method} {request.path}", "route", request.path, "denied",
                             result="403", reason=f"role '{current_user.role}' not in {list(roles)}")
                return abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def current_user_unauthorized():
    from .. import login_manager
    return login_manager.unauthorized()
