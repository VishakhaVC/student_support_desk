"""Audit trail writer. Only INSERTs exist here; the model + database triggers reject UPDATE and DELETE."""
import json

from ..extensions import db
from ..models import AuditLog
from ..security.redaction import redact

_NEVER_STORE = {"password", "otp", "token", "api_key", "message", "content", "secret"}


def _clean(details: dict | None) -> str | None:
    if not details:
        return None
    safe = {}
    for k, v in details.items():
        if k.lower() in _NEVER_STORE:
            continue
        safe[k] = redact(v)[0][:200] if isinstance(v, str) else v
    return json.dumps(safe, default=str)


def record(actor, action: str, target_type: str | None, target_id: str | None, decision: str, *, result: str | None = None,
           reason: str | None = None, trace_id: str | None = None, details: dict | None = None, commit: bool = True) -> AuditLog:
    entry = AuditLog(actor_user_id=getattr(actor, "id", None), actor_role=getattr(actor, "role", None) or "anonymous",
                     action=action, target_type=target_type, target_id=str(target_id) if target_id is not None else None,
                     decision=decision, result=redact(result)[0][:60] if result else None, reason=redact(reason or "")[0][:200] or None, trace_id=trace_id, details=_clean(details))
    db.session.add(entry)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return entry


def mask(value: str) -> str:
    """a***@d***.com style masking so audit rows do not hold full contact details."""
    if "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain[:1]}***{domain[domain.rfind('.'):]}"
    return f"{value[:3]}***{value[-2:]}" if len(value) > 6 else "***"
