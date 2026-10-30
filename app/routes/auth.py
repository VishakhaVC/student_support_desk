from datetime import timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for, current_app, session
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash

from ..models import AuditLog, User, utcnow
from ..services import audit

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("not-a-real-password")   # equalises timing for unknown usernames


def _locked_out(username: str) -> bool:
    cfg = current_app.config
    since = utcnow() - timedelta(minutes=cfg["LOGIN_LOCK_MINUTES"])
    fails = AuditLog.query.filter(AuditLog.action == "login", AuditLog.decision == "denied", AuditLog.target_id == username,
                                  AuditLog.timestamp >= since).count()
    return fails >= cfg["LOGIN_MAX_FAILURES"]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        username, password = request.form.get("username", "").strip()[:40], request.form.get("password", "")
        if _locked_out(username):
            audit.record(None, "login", "user", username, "denied", result="locked", reason="too many failed attempts")
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html"), 429
        user = User.query.filter_by(username=username).first()
        ok = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
        if user and ok and not user.is_active:
            audit.record(user, "login", "user", username, "denied", result="account_inactive", reason="account is deactivated")
            flash("This account is not active. Please contact the administration office.", "error")
            return render_template("login.html"), 403
        if user and ok:
            session.clear()                      # prevents session fixation
            login_user(user)
            audit.record(user, "login", "user", username, "allowed", result="success")
            return redirect(url_for("main.index"))
        audit.record(None, "login", "user", username, "denied", result="bad_credentials", reason="invalid username or password")
        flash("Invalid username or password.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit.record(current_user, "logout", "user", current_user.username, "allowed", result="success")
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
