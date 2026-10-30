"""Database models. All data is fictional demonstration data."""
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")  # student | admin
    display_name = db.Column(db.String(80), nullable=False)
    student_id = db.Column(db.String(20), db.ForeignKey("student_profiles.student_id"), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)   # False = account cannot sign in
    created_at = db.Column(db.DateTime, default=utcnow)

    # Flask-Login protocol
    is_anonymous = False

    @property
    def is_active(self):
        return bool(self.active)

    @property
    def is_authenticated(self):
        return True

    def get_id(self):
        return str(self.id)


class StudentProfile(db.Model):
    __tablename__ = "student_profiles"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    full_name = db.Column(db.String(80), nullable=False)
    programme = db.Column(db.String(120), nullable=False)
    trimester = db.Column(db.String(20), nullable=False)
    cgpa = db.Column(db.Float, nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="active")   # active | inactive | removed (soft delete)
    created_at = db.Column(db.DateTime, default=utcnow)


class FeeRecord(db.Model):
    __tablename__ = "fee_records"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), db.ForeignKey("student_profiles.student_id"), nullable=False, index=True)
    term = db.Column(db.String(40), nullable=False)
    total_fee = db.Column(db.Integer, nullable=False)
    paid = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.String(12), nullable=False)
    status = db.Column(db.String(30), nullable=False)  # Paid | Partially paid | Overdue

    @property
    def outstanding(self):
        return self.total_fee - self.paid


class ScholarshipRecord(db.Model):
    __tablename__ = "scholarship_records"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), db.ForeignKey("student_profiles.student_id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    percent = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), nullable=False)  # Active | Under review | Not applied
    condition = db.Column(db.String(200), nullable=False)
    next_review = db.Column(db.String(12), nullable=True)


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"
    id = db.Column(db.Integer, primary_key=True)
    ticket_ref = db.Column(db.String(20), unique=True, nullable=False)
    student_id = db.Column(db.String(20), db.ForeignKey("student_profiles.student_id"), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Open")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    # Idempotency: the same student sending the same request twice must not create two tickets.
    idempotency_key = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    last_response = db.Column(db.Text, nullable=True)          # latest administrator response shown to the student
    source = db.Column(db.String(12), nullable=False, default="web")   # web | assistant
    __table_args__ = (UniqueConstraint("student_id", "idempotency_key", name="uq_ticket_idem"),)


class TicketEvent(db.Model):
    """History of a support request: creation, status changes, responses, student replies."""
    __tablename__ = "ticket_events"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_tickets.id"), nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, nullable=True)
    actor_role = db.Column(db.String(20), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)   # created | status_changed | response | student_reply
    from_status = db.Column(db.String(20), nullable=True)
    to_status = db.Column(db.String(20), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class Conversation(db.Model):
    __tablename__ = "conversations"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(80), nullable=False, default="New conversation")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True)
    role = db.Column(db.String(12), nullable=False)  # user | assistant
    content = db.Column(db.Text, nullable=False)  # stored AFTER secret redaction
    run_id = db.Column(db.Integer, db.ForeignKey("agent_runs.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class MemoryItem(db.Model):
    """Long-term permitted memory. One value per (user, key) so newer facts replace stale ones."""
    __tablename__ = "memory_items"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key = db.Column(db.String(40), nullable=False)
    value = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_memory_user_key"),)


class AgentRun(db.Model):
    __tablename__ = "agent_runs"
    id = db.Column(db.Integer, primary_key=True)
    trace_id = db.Column(db.String(32), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=True)
    provider = db.Column(db.String(20), nullable=False)  # groq | deterministic
    model = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # completed | error | timeout | max_iterations | tool_limit
    error_kind = db.Column(db.String(30), nullable=True)
    iterations = db.Column(db.Integer, default=0)
    tool_call_count = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Float, nullable=True)
    input_tokens = db.Column(db.Integer, nullable=True)   # NULL = provider did not report usage
    output_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    context_json = db.Column(db.Text, nullable=True)      # which context sources were selected and why
    trace_json = db.Column(db.Text, nullable=True)        # ordered events of the run
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    tool_executions = db.relationship("ToolExecution", backref="run", cascade="all, delete-orphan")


class ToolExecution(db.Model):
    __tablename__ = "tool_executions"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("agent_runs.id"), nullable=False, index=True)
    step = db.Column(db.Integer, nullable=False)
    tool_name = db.Column(db.String(60), nullable=False)
    arguments_json = db.Column(db.Text, nullable=True)
    result_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False)  # ok | error | denied
    error_code = db.Column(db.String(40), nullable=True)
    latency_ms = db.Column(db.Float, nullable=True)


class ApprovalRequest(db.Model):
    """A sensitive change that must be approved by an administrator before it is applied."""
    __tablename__ = "approval_requests"
    id = db.Column(db.Integer, primary_key=True)
    approval_ref = db.Column(db.String(20), unique=True, nullable=False)
    requester_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    student_id = db.Column(db.String(20), db.ForeignKey("student_profiles.student_id"), nullable=False, index=True)
    action_type = db.Column(db.String(30), nullable=False, default="update_contact")
    field = db.Column(db.String(20), nullable=False)       # email | phone
    new_value = db.Column(db.String(120), nullable=False)
    reason = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending | approved | rejected
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_note = db.Column(db.String(200), nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)    # set exactly once, when the change is applied
    execution_result = db.Column(db.String(200), nullable=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_tickets.id"), nullable=True)   # optional link to a support request
    trace_id = db.Column(db.String(32), nullable=True)


class AuditLog(db.Model):
    """Append-only application-level audit trail (demonstration quality, NOT tamper-proof enterprise logging)."""
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, nullable=True)
    actor_role = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(60), nullable=False, index=True)
    target_type = db.Column(db.String(30), nullable=True)
    target_id = db.Column(db.String(40), nullable=True)
    decision = db.Column(db.String(12), nullable=False)    # allowed | denied | pending | approved | rejected | error
    result = db.Column(db.String(60), nullable=True)
    reason = db.Column(db.String(200), nullable=True)
    trace_id = db.Column(db.String(32), nullable=True)
    details = db.Column(db.Text, nullable=True)            # small JSON of non-sensitive metadata


class AuditImmutableError(Exception):
    pass


from sqlalchemy import DDL, event  # noqa: E402


@event.listens_for(AuditLog, "before_update")
def _audit_no_update(mapper, connection, target):
    raise AuditImmutableError("audit records cannot be modified")


@event.listens_for(AuditLog, "before_delete")
def _audit_no_delete(mapper, connection, target):
    raise AuditImmutableError("audit records cannot be deleted")


# Second layer: the database itself refuses UPDATE/DELETE on the audit table (SQLite triggers).
for _name, _op in (("audit_no_update", "UPDATE"), ("audit_no_delete", "DELETE")):
    event.listen(AuditLog.__table__, "after_create",
                 DDL(f"CREATE TRIGGER {_name} BEFORE {_op} ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;")
                 .execute_if(dialect="sqlite"))
