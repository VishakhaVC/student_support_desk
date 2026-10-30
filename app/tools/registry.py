"""Tool registry. Each tool has: a JSON-schema definition (what the LLM sees), a Pydantic model (what the backend
enforces), and an implementation that calls a service. The LLM can ask for a tool; only this code decides whether it runs."""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..models import ApprovalRequest
from ..security.authz import PermissionDenied
from ..services import approvals, audit, students, tickets

from ..services.validators import EMAIL as _EMAIL, PHONE as _PHONE


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(_Strict):
    pass


class SearchPolicyArgs(_Strict):
    query: str = Field(min_length=3, max_length=300)


class CreateTicketArgs(_Strict):
    category: Literal["payment", "scholarship", "course_change", "academic", "other"]
    subject: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=10, max_length=1000)


class GetTicketsArgs(_Strict):
    limit: int = Field(default=5, ge=1, le=10)
    status: Optional[Literal["Open", "Under Review", "Awaiting Student", "Resolved", "Rejected", "Closed"]] = None


class ApproveArgs(_Strict):
    approval_ref: str = Field(pattern=r"^APR-\d{4,}$")


IDENTITY_KEYS = {"student_id", "user_id", "id", "username", "student", "target_student_id", "studentid", "roll_no"}


class ProfileChangeArgs(_Strict):
    field: Literal["full_name", "email", "phone"]
    new_value: str = Field(min_length=2, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _format(self):
        if self.field == "full_name" and not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,78}", self.new_value):
            raise ValueError("new_value is not a valid full name")
        if self.field == "email" and not _EMAIL.match(self.new_value):
            raise ValueError("new_value is not a valid email address")
        if self.field == "phone" and not _PHONE.match(self.new_value):
            raise ValueError("new_value is not a valid phone number")
        return self


@dataclass
class ToolContext:
    user: Any
    config: dict
    retriever: Any
    trace_id: str
    evidence: list = field(default_factory=list)  # policy chunks actually retrieved during this run


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    model: type
    func: Callable
    roles: tuple = ("student",)
    audit_action: str | None = None   # successful use is written to the audit log

    def definition(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ---------------- tool implementations ----------------
def _fee(ctx, a):
    data = students.get_fee_status(ctx.user, trace_id=ctx.trace_id)
    return {"ok": True, "fee": data} if data else {"ok": False, "error": "no_record", "message": "No fee record found for this student."}


def _scholarship(ctx, a):
    data = students.get_scholarship_status(ctx.user, trace_id=ctx.trace_id)
    return ({"ok": True, "scholarship": data} if data else
            {"ok": True, "scholarship": None, "message": "No scholarship is recorded for this student."})


def _policy(ctx, a: SearchPolicyArgs):
    res = ctx.retriever.search(a.query)
    ctx.evidence.extend({"chunk_id": c["chunk_id"], "section": c["section"], "score": c["score"]} for c in res["chunks"])
    return {"ok": True, **res}


def _create_ticket(ctx, a: CreateTicketArgs):
    return {"ok": True, "ticket": tickets.create_ticket(ctx.user, a.category, a.subject, a.description, source="assistant")}


def _get_tickets(ctx, a: GetTicketsArgs):
    return {"ok": True, "tickets": tickets.list_tickets(ctx.user, a.limit, a.status, trace_id=ctx.trace_id)}


def _profile_change(ctx, a: ProfileChangeArgs):
    # The agent can only create a PENDING request. The student record is not touched until an administrator approves.
    return {"ok": True, **approvals.request_contact_change(ctx.user, a.field, a.new_value, a.reason, ctx.trace_id)}


def _approve(ctx, a: ApproveArgs):
    req = ApprovalRequest.query.filter_by(approval_ref=a.approval_ref).first()
    if not req:
        return {"ok": False, "error": "not_found", "message": "No such approval request."}
    try:
        done = approvals.decide(ctx.user, req.id, True, trace_id=ctx.trace_id)
    except approvals.ApprovalStateError as e:
        return {"ok": False, "error": str(e), "message": "The request is not pending."}
    return {"ok": True, "status": done.status, "approval_ref": done.approval_ref}


TOOLS: list[ToolSpec] = [
    ToolSpec("get_my_fee_status",
             "Get the signed-in student's own fee status: total fee, amount paid, outstanding balance, due date. "
             "The student is identified automatically; do not pass any student ID.",
             _obj({}, []), NoArgs, _fee, audit_action="tool:view_fee_status"),
    ToolSpec("get_my_scholarship_status",
             "Get the signed-in student's own scholarship record: scholarship name, percentage, status, condition, next review date. "
             "The student is identified automatically; do not pass any student ID.",
             _obj({}, []), NoArgs, _scholarship, audit_action="tool:view_scholarship_status"),
    ToolSpec("search_student_policy",
             "Search the student handbook for rules, deadlines, eligibility conditions and procedures (fees, payment portal failures, "
             "scholarships, course changes, academic support, support requests). Returns only the few relevant excerpts with section ids.",
             _obj({"query": {"type": "string", "description": "What to look up, in a short phrase"}}, ["query"]), SearchPolicyArgs, _policy),
    ToolSpec("create_support_ticket",
             "Create a support request for the signed-in student. Only call when the student asks to raise a request and you know the issue. "
             "Repeating the same request does not create a duplicate.",
             _obj({"category": {"type": "string", "enum": ["payment", "scholarship", "course_change", "academic", "other"]},
                   "subject": {"type": "string", "description": "Short title, 5-120 characters"},
                   "description": {"type": "string", "description": "What the problem is, 10-1000 characters"}},
                  ["category", "subject", "description"]), CreateTicketArgs, _create_ticket, audit_action="tool:create_support_ticket"),
    ToolSpec("get_my_support_tickets",
             "List the signed-in student's own support requests, newest first, with each request's status and the latest administrator response.",
             _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 10},
                   "status": {"type": "string", "enum": ["Open", "Under Review", "Awaiting Student", "Resolved", "Rejected", "Closed"]}}, []), GetTicketsArgs, _get_tickets, audit_action="tool:view_tickets"),
    ToolSpec("request_profile_change",
             "Request a change to the signed-in student's registered name, email or phone number. The change is NOT applied immediately: it "
             "creates a request that staff must approve. Only call when the student has given the exact new value; if missing, ask for it.",
             _obj({"field": {"type": "string", "enum": ["full_name", "email", "phone"]},
                   "new_value": {"type": "string", "description": "The new full name, email address or phone number"},
                   "reason": {"type": "string"}}, ["field", "new_value"]), ProfileChangeArgs, _profile_change),
    ToolSpec("approve_approval_request",
             "ADMINISTRATOR ONLY. Approve a pending approval request by its reference (e.g. APR-0001).",
             _obj({"approval_ref": {"type": "string"}}, ["approval_ref"]), ApproveArgs, _approve, roles=("admin",)),
]


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec] = TOOLS):
        self.tools = {t.name: t for t in tools}

    def definitions_for(self, user) -> list[dict]:
        return [t.definition() for t in self.tools.values() if user.role in t.roles]

    def execute(self, name: str, raw_args: str, ctx: ToolContext) -> dict:
        """Always returns an outcome dict (never raises): name, arguments, result, status, error_code, latency_ms."""
        t0 = time.perf_counter()

        def outcome(result, status, code=None):
            return {"name": name, "arguments": raw_args, "result": result, "status": status, "error_code": code,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}

        spec = self.tools.get(name)
        if spec is None:
            return outcome({"ok": False, "error": "unknown_tool", "message": f"No tool named '{name}'."}, "error", "unknown_tool")
        if ctx.user.role not in spec.roles:   # least privilege: enforced here regardless of what the model was told
            audit.record(ctx.user, f"tool:{name}", "tool", name, "denied", result="blocked", reason=f"role '{ctx.user.role}' may not use this tool", trace_id=ctx.trace_id)
            return outcome({"ok": False, "error": "forbidden", "message": "You are not permitted to use that tool."}, "denied", "forbidden")
        try:
            args = json.loads(raw_args) if raw_args and raw_args.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, json.JSONDecodeError):
            return outcome({"ok": False, "error": "invalid_json", "message": "Tool arguments were not valid JSON."}, "error", "invalid_json")
        smuggled = sorted(k for k in args if k.lower() in IDENTITY_KEYS)
        if smuggled:  # the model must never choose whose data is accessed; identity comes from the session only
            audit.record(ctx.user, f"tool:{name}", "tool", name, "denied", result="blocked", trace_id=ctx.trace_id,
                         reason=f"model supplied identity argument(s) {smuggled}")
            return outcome({"ok": False, "error": "identity_override_blocked", "message": "Identity is taken from the signed-in session and cannot be supplied."},
                           "denied", "identity_override_blocked")
        try:
            parsed = spec.model(**args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'arguments'}: {x['msg']}" for x in e.errors())
            return outcome({"ok": False, "error": "invalid_arguments", "message": problems}, "error", "invalid_arguments")
        try:
            if name in ctx.config.get("INJECT_TOOL_FAILURES", ()):
                raise RuntimeError("Simulated backend outage (failure injection for evaluation)")
            result = spec.func(ctx, parsed)
        except ValueError as e:
            return outcome({"ok": False, "error": "invalid_arguments", "message": str(e)}, "error", "invalid_arguments")
        except PermissionDenied as e:
            return outcome({"ok": False, "error": "permission_denied", "message": "You are not permitted to do that."}, "denied", e.reason)
        except Exception as e:  # tool failure: reported to the model as an observation, never crashes the run
            from ..extensions import db
            db.session.rollback()
            return outcome({"ok": False, "error": "tool_failure", "message": f"The tool failed: {type(e).__name__}"}, "error", "tool_failure")
        if spec.audit_action:
            audit.record(ctx.user, spec.audit_action, "student", ctx.user.student_id, "allowed" if result.get("ok") else "error",
                         result="ok" if result.get("ok") else result.get("error"), trace_id=ctx.trace_id)
        return outcome(result, "ok" if result.get("ok") else "error", None if result.get("ok") else result.get("error"))
