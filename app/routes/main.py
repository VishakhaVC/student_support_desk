import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..memory import store as memory
from ..models import ApprovalRequest, SupportTicket, TicketEvent
from ..security.authz import PermissionDenied, role_required
from ..services import approvals, audit, students, tickets
from ..services.validators import EMAIL, PHONE

bp = Blueprint("main", __name__)

STYLES = ["short", "detailed", "simple", "concise", "step-by-step"]


@bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    return redirect(url_for("admin.dashboard") if current_user.role == "admin" else url_for("main.dashboard"))


def _activity(student_id: str, limit: int = 6) -> list[dict]:
    """Recent activity built from real records: request history and approval decisions."""
    items = []
    rows = (db.session.query(TicketEvent, SupportTicket).join(SupportTicket, SupportTicket.id == TicketEvent.ticket_id)
            .filter(SupportTicket.student_id == student_id).order_by(TicketEvent.id.desc()).limit(limit).all())
    for e, t in rows:
        text = {"created": f"You raised {t.ticket_ref}: {t.subject}",
                "status_changed": f"{t.ticket_ref} moved to {e.to_status}",
                "response": f"Administration replied on {t.ticket_ref}",
                "student_reply": f"You replied on {t.ticket_ref}"}.get(e.event_type, t.ticket_ref)
        items.append({"time": e.created_at, "text": text, "link": url_for("main.ticket_detail", ref=t.ticket_ref)})
    for a in ApprovalRequest.query.filter_by(student_id=student_id).order_by(ApprovalRequest.id.desc()).limit(limit).all():
        verb = {"pending": "is waiting for review", "approved": "was approved", "rejected": "was rejected"}[a.status]
        items.append({"time": a.decided_at or a.created_at, "text": f"{a.approval_ref}: change of registered {a.field} {verb}",
                      "link": url_for("main.my_approvals")})
    return sorted(items, key=lambda i: i["time"], reverse=True)[:limit]


@bp.route("/dashboard")
@role_required("student")
def dashboard():
    sid = current_user.student_id
    return render_template("dashboard.html", profile=students.get_profile(current_user), fee=students.get_fee_status(current_user),
                           scholarship=students.get_scholarship_status(current_user), counts=tickets.counts(sid),
                           pending=ApprovalRequest.query.filter_by(student_id=sid, status="pending").count(),
                           recent=tickets.list_tickets(current_user, 5), activity=_activity(sid))


@bp.route("/assistant")
@role_required("student")
def assistant():
    suggestions = ["What is my current fee status?", "Am I eligible for a scholarship?", "What happens if my payment portal fails?",
                   "How can I request a course change?", "Create a support request for my payment issue."]
    return render_template("assistant.html", suggestions=suggestions)


# ---------------------------------------------------------------- profile
@bp.route("/profile")
@role_required("student")
def profile():
    p = students.get_profile(current_user)
    pending = {a.field: a for a in ApprovalRequest.query.filter_by(student_id=current_user.student_id, status="pending").all()}
    mem = {m.key: m.value for m in memory.list_items(current_user.id)}
    return render_template("profile.html", profile=p, pending=pending, mem=mem, languages=sorted(l.capitalize() for l in memory.LANGUAGES),
                           styles=STYLES, fee=students.get_fee_status(current_user), scholarship=students.get_scholarship_status(current_user))


@bp.route("/profile/preferences", methods=["POST"])
@role_required("student")
def save_preferences():
    changed = []
    for key, value in (("preferred_language", request.form.get("preferred_language", "").strip()),
                       ("response_style", request.form.get("response_style", "").strip()),
                       ("preferred_name", request.form.get("preferred_name", "").strip())):
        if not value:
            memory.delete(current_user.id, key)
        elif re.fullmatch(r"[A-Za-z\-]{2,20}", value) and memory.save(current_user.id, key, value):
            changed.append(key)
        else:
            flash(f"“{value}” is not a valid value for {key.replace('_', ' ')}.", "error")
            return redirect(url_for("main.profile"))
    audit.record(current_user, "update_preferences", "memory", current_user.student_id, "allowed", result=",".join(changed) or "cleared")
    flash("Preferences saved.", "ok")
    return redirect(url_for("main.profile"))


def _valid_contact(field: str, value: str) -> str | None:
    if field == "email" and not EMAIL.match(value):
        return "Enter a valid email address."
    if field == "phone" and not PHONE.match(value):
        return "Enter a valid phone number (8-15 digits, optional +)."
    if field == "full_name" and not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,78}", value):
        return "Enter a valid full name (2–79 characters)."
    if field not in ("full_name", "email", "phone"):
        return "Choose name, email or phone."
    return None


@bp.route("/profile/contact-change", methods=["POST"])
@role_required("student")
def request_contact_change():
    field, value = request.form.get("field", ""), request.form.get("new_value", "").strip()
    problem = _valid_contact(field, value)
    if problem:
        flash(problem, "error")
    else:
        try:
            r = approvals.request_contact_change(current_user, field, value, "Requested from My Profile", None)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.profile"))
        flash(f"Request {r['approval_ref']} sent for staff approval. Your current {field} stays in place until it is approved.", "ok")
    return redirect(url_for("main.my_approvals"))


@bp.route("/profile/password", methods=["POST"])
@role_required("student")
def change_password():
    from werkzeug.security import check_password_hash, generate_password_hash
    from ..services.validators import password_problems
    old = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    verify = request.form.get("confirm_password", "")
    if not check_password_hash(current_user.password_hash, old):
        flash("Current password is incorrect.", "error")
    elif new != verify:
        flash("The new passwords do not match.", "error")
    elif password_problems(new):
        flash("Your new password needs " + ", ".join(password_problems(new)) + ".", "error")
    elif check_password_hash(current_user.password_hash, new):
        flash("Choose a password different from your current one.", "error")
    else:
        current_user.password_hash = generate_password_hash(new)
        audit.record(current_user, "change_password", "user", str(current_user.id), "allowed", result="updated", commit=False)
        db.session.commit()
        flash("Your password has been updated.", "ok")
    return redirect(url_for("main.profile"))


# ---------------------------------------------------------------- support requests
@bp.route("/tickets", methods=["GET", "POST"])
@role_required("student")
def tickets_page():
    if request.method == "POST":
        category = request.form.get("category", "other")
        subject, desc = request.form.get("subject", "").strip(), request.form.get("description", "").strip()
        field, value = request.form.get("field", ""), request.form.get("new_value", "").strip()
        if category == "contact_change":
            problem = _valid_contact(field, value)
            subject = subject or f"Update registered {field}"
            desc = desc or f"Please update my registered {field} to {value}."
            if problem:
                flash(problem, "error")
                return redirect(url_for("main.tickets_page"))
        if category not in tickets.CATEGORIES or not (5 <= len(subject) <= 120) or not (10 <= len(desc) <= 1000):
            flash("Choose a category, a subject (5-120 characters) and a description (10-1000 characters).", "error")
            return redirect(url_for("main.tickets_page"))
        t = tickets.create_ticket(current_user, category, subject, desc)
        if t["duplicate"]:
            flash(f"You already raised this request as {t['ticket_ref']}.", "error")
            return redirect(url_for("main.ticket_detail", ref=t["ticket_ref"]))
        audit.record(current_user, "create_support_ticket", "ticket", t["ticket_ref"], "allowed", result="created")
        if category == "contact_change":   # the sensitive change itself still needs administrator approval
            ticket = SupportTicket.query.filter_by(ticket_ref=t["ticket_ref"]).first()
            try:
                a = approvals.request_contact_change(current_user, field, value, subject, None, ticket_id=ticket.id)
                flash(f"{t['ticket_ref']} created and approval {a['approval_ref']} requested. Your details change only after approval.", "ok")
            except ValueError as exc:
                flash(f"Ticket {t['ticket_ref']} was created, but an approval was not linked: {exc}", "error")
        else:
            flash(f"Support request {t['ticket_ref']} created.", "ok")
        return redirect(url_for("main.ticket_detail", ref=t["ticket_ref"]))
    status = request.args.get("status", "")
    status = status if status in ("active", *tickets.STATUSES) else ""
    return render_template("tickets.html", tickets=tickets.list_tickets(current_user, 100, status or None), status=status,
                           categories=tickets.CATEGORIES, statuses=tickets.STATUSES, counts=tickets.counts(current_user.student_id))


@bp.route("/tickets/<ref>/request-change", methods=["POST"])
@role_required("student")
def ticket_request_change(ref):
    # Explicit student authorization is required: text within a generic ticket never executes a profile change.
    try:
        ticket = tickets.get_ticket(current_user, ref)
    except tickets.TicketError:
        return render_template("error.html", code=404, title="Not found", text="That request does not exist."), 404
    if ticket.status not in tickets.ACTIVE_STATUSES:
        flash("Reopen this support request before linking a new approval.", "error")
        return redirect(url_for("main.ticket_detail", ref=ref))
    field = request.form.get("field", "")
    value = request.form.get("new_value", "").strip()
    problem = _valid_contact(field, value)
    if problem:
        flash(problem, "error")
        return redirect(url_for("main.ticket_detail", ref=ref))
    linked = ApprovalRequest.query.filter_by(ticket_id=ticket.id).first()
    if linked:
        flash("This ticket already has a linked approval request.", "error")
        return redirect(url_for("main.ticket_detail", ref=ref))
    try:
        result = approvals.request_contact_change(current_user, field, value, ticket.subject, None, ticket_id=ticket.id)
        flash(f"Approval {result['approval_ref']} linked to {ticket.ticket_ref}. Staff can review it in Approvals.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.ticket_detail", ref=ref))


@bp.route("/tickets/<ref>")
@role_required("student")
def ticket_detail(ref):
    try:
        t = tickets.get_ticket(current_user, ref)
    except tickets.TicketError:
        return render_template("error.html", code=404, title="Not found", text="That request does not exist."), 404
    linked = ApprovalRequest.query.filter_by(ticket_id=t.id).first()
    return render_template("ticket_detail.html", t=t, events=tickets.history(t), linked=linked, active=tickets.ACTIVE_STATUSES)


@bp.route("/tickets/<ref>/reply", methods=["POST"])
@role_required("student")
def ticket_reply(ref):
    try:
        tickets.student_reply(current_user, ref, request.form.get("message", ""))
        flash("Your reply was sent.", "ok")
    except tickets.TicketError as e:
        if e.code == "not_found":
            return render_template("error.html", code=404, title="Not found", text="That request does not exist."), 404
        flash(e.message, "error")
    return redirect(url_for("main.ticket_detail", ref=ref))


@bp.route("/approvals")
@role_required("student")
def my_approvals():
    items = approvals.for_student(current_user)
    refs = {t.id: t.ticket_ref for t in SupportTicket.query.filter(SupportTicket.id.in_([a.ticket_id for a in items if a.ticket_id])).all()} if items else {}
    return render_template("approvals.html", items=items, ticket_refs=refs)
