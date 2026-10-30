"""The upgrade path for an existing database created by the earlier version of the application."""
from flask import Flask
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.migrations import pending_columns, upgrade_schema

LEGACY = [
    "CREATE TABLE student_profiles (id INTEGER PRIMARY KEY, student_id VARCHAR(20) UNIQUE NOT NULL, full_name VARCHAR(80) NOT NULL, programme VARCHAR(120) NOT NULL, trimester VARCHAR(20) NOT NULL, cgpa FLOAT NOT NULL, email VARCHAR(120) NOT NULL, phone VARCHAR(30) NOT NULL)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(40) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, display_name VARCHAR(80) NOT NULL, student_id VARCHAR(20))",
    "CREATE TABLE support_tickets (id INTEGER PRIMARY KEY, ticket_ref VARCHAR(20) UNIQUE NOT NULL, student_id VARCHAR(20) NOT NULL, category VARCHAR(30) NOT NULL, subject VARCHAR(120) NOT NULL, description TEXT NOT NULL, status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, idempotency_key VARCHAR(64) NOT NULL)",
    "CREATE TABLE approval_requests (id INTEGER PRIMARY KEY, approval_ref VARCHAR(20) UNIQUE NOT NULL, requester_user_id INTEGER NOT NULL, student_id VARCHAR(20) NOT NULL, action_type VARCHAR(30) NOT NULL, field VARCHAR(20) NOT NULL, new_value VARCHAR(120) NOT NULL, reason VARCHAR(200), status VARCHAR(12) NOT NULL, created_at DATETIME NOT NULL, decided_by_user_id INTEGER, decided_at DATETIME, decision_note VARCHAR(200), executed_at DATETIME, trace_id VARCHAR(32))",
]


def test_legacy_database_is_upgraded_without_losing_data(tmp_path):
    path = tmp_path / "legacy.db"
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{path}"
    db.init_app(app)
    with app.app_context():
        for ddl in LEGACY:
            db.session.execute(text(ddl))
        db.session.execute(text("INSERT INTO student_profiles VALUES (1,'S1','Old Student','PGDM','Trimester I',3.0,'o@x.com','+91 90000 00001')"))
        db.session.execute(text("INSERT INTO users VALUES (1,'old_user',:h,'student','Old Student','S1')"), {"h": generate_password_hash("Legacy#Pass1")})
        db.session.execute(text("INSERT INTO support_tickets VALUES (1,'TKT-0001','S1','payment','Old issue','details here',  'In progress','2026-10-01 10:00:00','k1')"))
        db.session.execute(text("INSERT INTO approval_requests VALUES (1,'APR-0001',1,'S1','update_contact','email','n@x.com',NULL,'pending','2026-10-02 10:00:00',NULL,NULL,NULL,NULL,NULL)"))
        db.session.commit()
        assert len(pending_columns()) == 10
        summary = upgrade_schema(backup_dir=tmp_path / "bk")
        assert len(summary["columns_added"]) == 10 and summary["backup"] and list((tmp_path / "bk").glob("*.bak"))
        assert pending_columns() == [] and inspect(db.engine).has_table("ticket_events") and inspect(db.engine).has_table("audit_logs")
        row = db.session.execute(text("SELECT active, status FROM users u JOIN student_profiles p ON p.student_id=u.student_id")).one()
        assert row[0] == 1 and row[1] == "active"
        from app.models import SupportTicket, TicketEvent, User, ApprovalRequest
        u = User.query.filter_by(username="old_user").one()
        assert check_password_hash(u.password_hash, "Legacy#Pass1") and u.is_active
        t = SupportTicket.query.one()
        assert t.status == "Under Review" and t.updated_at is not None and t.source == "web"
        assert TicketEvent.query.filter_by(ticket_id=t.id, event_type="created").count() == 1
        assert ApprovalRequest.query.one().status == "pending"
        again = upgrade_schema()                                                   # safe to run twice
        assert again["columns_added"] == [] and again["events_backfilled"] == 0 and again["backup"] is None
