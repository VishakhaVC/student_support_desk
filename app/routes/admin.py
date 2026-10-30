import json

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import AgentRun, ApprovalRequest, AuditLog, SupportTicket, TicketEvent
from ..security.authz import PermissionDenied, role_required
from ..services import approvals, audit, performance, student_admin, tickets
from ..services.validators import TRIMESTERS, ValidationError

bp = Blueprint("admin", __name__, url_prefix="/admin")
admin_only = role_required("admin")


# ---------------------------------------------------------------- dashboard
@bp.route("/")
@admin_only
def dashboard():
    tc = tickets.counts()
    events = (db.session.query(TicketEvent, SupportTicket).join(SupportTicket, SupportTicket.id == TicketEvent.ticket_id)
              .order_by(TicketEvent.id.desc()).limit(6).all())
    return render_template("admin.html", students=student_admin.counts(), tc=tc,
                           pending=approvals.pending()[:5], pending_total=ApprovalRequest.query.filter_by(status="pending").count(),
                           open_tickets=SupportTicket.query.filter(SupportTicket.status.in_(tickets.ACTIVE_STATUSES)).order_by(SupportTicket.id.desc()).limit(6).all(),
                           events=events, decided=ApprovalRequest.query.filter(ApprovalRequest.status != "pending").order_by(ApprovalRequest.id.desc()).limit(5).all())


# ---------------------------------------------------------------- students
@bp.route("/students")
@admin_only
def students_page():
    q, status, programme = request.args.get("q", "").strip(), request.args.get("status", ""), request.args.get("programme", "")
    rows = student_admin.list_students(q, status or None, programme or None)
    return render_template("students.html", rows=rows, q=q, status=status, programme=programme, programmes=student_admin.programmes(),
                           counts=student_admin.counts(), accounts={r.student_id: student_admin.account_for(r.student_id) for r in rows})


def _form_data():
    return {k: request.form.get(k, "") for k in ("student_id", "username", "full_name", "email", "phone", "programme", "trimester", "cgpa",
                                                    "status", "password", "total_fee", "due_date")}


@bp.route("/students/new", methods=["GET", "POST"])
@admin_only
def student_new():
    errors, data = {}, {"status": "active", "trimester": "Trimester I", "total_fee": "450000"}
    if request.method == "POST":
        data = _form_data()
        try:
            profile, generated = student_admin.create_student(current_user, data)
            return render_template("student_detail.html", **_detail(profile.student_id), one_time_password=generated or "(the password you entered)",
                                   one_time_note="Student created.")
        except ValidationError as e:
            errors = e.errors
            flash("Please correct the highlighted fields.", "error")
    return render_template("student_form.html", errors=errors, data=data, trimesters=TRIMESTERS, programmes=student_admin.programmes())


def _detail(sid):
    return {**student_admin.overview(sid), "trimesters": TRIMESTERS, "errors": {}}


@bp.route("/students/<student_id>")
@admin_only
def student_detail(student_id):
    ov = student_admin.overview(student_id)
    if not ov:
        return render_template("error.html", code=404, title="Not found", text="No such student."), 404
    audit.record(current_user, "admin_view_student", "student", student_id, "allowed", result="viewed")
    return render_template("student_detail.html", **ov, trimesters=TRIMESTERS, errors={})


@bp.route("/students/<student_id>/edit", methods=["POST"])
@admin_only
def student_edit(student_id):
    try:
        student_admin.update_student(current_user, student_id, _form_data())
        flash("Student details updated.", "ok")
    except ValidationError as e:
        flash("Not saved: " + "; ".join(e.errors.values()), "error")
    return redirect(url_for("admin.student_detail", student_id=student_id))


@bp.route("/students/<student_id>/status", methods=["POST"])
@admin_only
def student_status(student_id):
    status = request.form.get("status", "")
    try:
        student_admin.set_status(current_user, student_id, status)
        flash({"active": "Account reactivated.", "inactive": "Account deactivated. The student can no longer sign in.",
               "removed": "Student removed. The record is kept for the audit trail."}[status], "ok")
    except (ValidationError, KeyError) as e:
        flash("Could not change the status.", "error")
    return redirect(url_for("admin.students_page") if status == "removed" else url_for("admin.student_detail", student_id=student_id))


@bp.route("/students/<student_id>/reset-password", methods=["POST"])
@admin_only
def student_reset_password(student_id):
    try:
        pw = student_admin.reset_password(current_user, student_id, request.form.get("password") or None)
    except ValidationError as e:
        flash("Not reset: " + "; ".join(e.errors.values()), "error")
        return redirect(url_for("admin.student_detail", student_id=student_id))
    return render_template("student_detail.html", **_detail(student_id), one_time_password=pw,
                           one_time_note="Password reset.")


# ---------------------------------------------------------------- support requests
@bp.route("/tickets")
@admin_only
def tickets_page():
    status, category, q = request.args.get("status", ""), request.args.get("category", ""), request.args.get("q", "").strip()
    query = SupportTicket.query
    if status == "active":
        query = query.filter(SupportTicket.status.in_(tickets.ACTIVE_STATUSES))
    elif status in tickets.STATUSES:
        query = query.filter_by(status=status)
    if category in tickets.CATEGORIES:
        query = query.filter_by(category=category)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(SupportTicket.ticket_ref.like(like), SupportTicket.subject.like(like), SupportTicket.student_id.like(like)))
    from ..models import StudentProfile
    names = {p.student_id: p.full_name for p in StudentProfile.query.all()}
    return render_template("admin_tickets.html", rows=query.order_by(SupportTicket.updated_at.desc()).limit(300).all(), names=names, status=status,
                           category=category, q=q, categories=tickets.CATEGORIES, statuses=tickets.STATUSES, counts=tickets.counts())


@bp.route("/tickets/<ref>")
@admin_only
def ticket_detail(ref):
    try:
        t = tickets.get_ticket(current_user, ref)
    except tickets.TicketError:
        return render_template("error.html", code=404, title="Not found", text="That request does not exist."), 404
    from ..models import StudentProfile
    return render_template("admin_ticket_detail.html", t=t, events=tickets.history(t), student=StudentProfile.query.filter_by(student_id=t.student_id).first(),
                           linked=ApprovalRequest.query.filter_by(ticket_id=t.id).first(), transitions=sorted(tickets.TRANSITIONS.get(t.status, set())),
                           active=tickets.ACTIVE_STATUSES)


@bp.route("/tickets/<ref>/update", methods=["POST"])
@admin_only
def ticket_update(ref):
    try:
        tickets.admin_update(current_user, ref, request.form.get("status", ""), request.form.get("note"))
        flash("Request updated. The student can see your response.", "ok")
    except tickets.TicketError as e:
        flash(e.message, "error")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/admin/") else url_for("admin.ticket_detail", ref=ref))


# ---------------------------------------------------------------- approvals
@bp.route("/approvals")
@admin_only
def approvals_page():
    status, q = request.args.get("status", "pending"), request.args.get("q", "").strip()
    from ..models import StudentProfile
    names = {p.student_id: p.full_name for p in StudentProfile.query.all()}
    rows = approvals.list_all(status if status in ("pending", "approved", "rejected") else None, q)
    linked_ids = [a.ticket_id for a in rows if a.ticket_id is not None]
    ticket_refs = {t.id: t.ticket_ref for t in SupportTicket.query.filter(SupportTicket.id.in_(linked_ids)).all()} if linked_ids else {}
    return render_template("admin_approvals.html", rows=rows, ticket_refs=ticket_refs,
                           status=status, q=q, names=names,
                           counts={s: ApprovalRequest.query.filter_by(status=s).count() for s in ("pending", "approved", "rejected")})


@bp.route("/approvals/<int:approval_id>/decide", methods=["POST"])
@admin_only
def decide(approval_id):
    approve = request.form.get("decision") == "approve"
    note = (request.form.get("note") or "").strip()
    if not approve and len(note) < 3:
        flash("Please give a reason when rejecting a request.", "error")
        return redirect(url_for("admin.approvals_page"))
    try:
        req = approvals.decide(current_user, approval_id, approve, note)
        flash(f"{req.approval_ref} {'approved and applied' if approve else 'rejected; no change was made'}.", "ok")
    except PermissionDenied as e:
        flash(f"Blocked: {e.reason.replace('_', ' ')}.", "error")
    except approvals.ApprovalStateError:
        flash("That request is no longer pending, so nothing was changed.", "error")
    except ValueError as e:
        flash(f"Could not apply the change: {e}.", "error")
    return redirect(url_for("admin.approvals_page"))


# ---------------------------------------------------------------- AI performance, runs, logs
@bp.route("/performance")
@admin_only
def performance_page():
    return render_template("performance.html", live=performance.live_usage(), ev=performance.evaluations(current_app.config["RESULTS_DIR"]),
                           runs=performance.recent_runs(10))


@bp.route("/runs/<int:run_id>")
@admin_only
def run_detail(run_id):
    run = AgentRun.query.get_or_404(run_id)
    audits = AuditLog.query.filter_by(trace_id=run.trace_id).order_by(AuditLog.id).all()
    return render_template("run_detail.html", run=run, events=json.loads(run.trace_json or "[]"), context=json.loads(run.context_json or "[]"), audits=audits)


@bp.route("/logs")
@admin_only
def logs_page():
    q = AuditLog.query
    args = {k: request.args.get(k, "").strip() for k in ("action", "decision", "actor_role")}
    if args["action"]:
        q = q.filter(AuditLog.action.like(f"{args['action']}%"))
    if args["decision"]:
        q = q.filter(AuditLog.decision == args["decision"])
    if args["actor_role"]:
        q = q.filter(AuditLog.actor_role == args["actor_role"])
    return render_template("audit.html", rows=q.order_by(AuditLog.id.desc()).limit(200).all(), args=args)


# Old URLs keep working.
@bp.route("/audit")
@admin_only
def _old_audit():
    return redirect(url_for("admin.logs_page", **request.args))


@bp.route("/evaluation")
@bp.route("/runs")
@admin_only
def _old_eval():
    return redirect(url_for("admin.performance_page"))
