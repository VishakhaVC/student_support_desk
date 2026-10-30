from flask import Flask, jsonify, render_template, request
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from .config import Config
from .extensions import db

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.session_protection = "strong"
csrf = CSRFProtect()


def create_app(config_class=Config, **overrides):
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.config.update(overrides)
    if app.config["LLM_MODE"] != "groq" and not app.config.get("TESTING"):
        raise RuntimeError("LLM_MODE must be 'groq' for the web application (other modes exist only for automated tests and evaluation).")

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .models import User
    from .retrieval.policy import PolicyRetriever, load_chunks
    from .tools.registry import ToolRegistry

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.extensions["retriever"] = PolicyRetriever(load_chunks(app.config["POLICY_FILE"]), app.config["POLICY_TOP_K"])
    app.extensions["registry"] = ToolRegistry()

    from .routes.admin import bp as admin_bp
    from .routes.api import bp as api_bp
    from .routes.auth import bp as auth_bp
    from .routes.main import bp as main_bp
    for bp in (auth_bp, main_bp, admin_bp, api_bp):
        app.register_blueprint(bp)

    def _error(code, title, text):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": title.lower().replace(" ", "_"), "message": text}), code
        return render_template("error.html", code=code, title=title, text=text), code

    app.register_error_handler(403, lambda e: _error(403, "Not permitted", "Your account does not have permission to do that."))
    app.register_error_handler(404, lambda e: _error(404, "Not found", "That page or record does not exist."))
    app.register_error_handler(401, lambda e: _error(401, "Sign in required", "Please sign in first."))
    app.register_error_handler(400, lambda e: _error(400, "Bad request", getattr(e, "description", "The request could not be processed.")))

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        counts = {}
        if current_user.is_authenticated:
            from .models import ApprovalRequest, SupportTicket
            from .services import tickets as tsvc
            if current_user.role == "admin":
                counts = {"approvals": ApprovalRequest.query.filter_by(status="pending").count(),
                          "tickets": SupportTicket.query.filter(SupportTicket.status.in_(tsvc.ACTIVE_STATUSES)).count()}
            elif current_user.student_id:
                counts = {"approvals": ApprovalRequest.query.filter_by(student_id=current_user.student_id, status="pending").count(),
                          "tickets": tsvc.counts(current_user.student_id)["open"]}
        return {"nav_counts": counts}

    with app.app_context():
        if app.config.get("TESTING"):
            db.create_all()
        else:
            from .migrations import upgrade_schema
            summary = upgrade_schema()
            if summary["columns_added"]:
                print(f"Database upgraded (backup: {summary['backup']}): {', '.join(summary['columns_added'])}")
            if User.query.count() == 0:
                from data.seed.seed import seed_database, write_login_file
                creds = seed_database()
                write_login_file(creds, app.config["LOGIN_FILE"])
                print(f"New database created. Local sign-in details were written to {app.config['LOGIN_FILE'].name} (not tracked by Git).")

    @app.before_request
    def _end_session_of_inactive_accounts():
        from flask_login import current_user, logout_user
        if current_user.is_authenticated and not current_user.is_active:
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "account_inactive", "message": "This account is no longer active."}), 401
            from flask import flash, redirect, url_for
            flash("This account is no longer active. Please contact the administration office.", "error")
            return redirect(url_for("auth.login"))

    @app.cli.command("reset-db")
    def reset_db():
        """Drop all tables and re-create the deterministic demo data."""
        from data.seed.seed import seed_database
        db.drop_all()
        db.create_all()
        seed_database()
        print("Database reset and seeded with fictional demo data.")

    return app
