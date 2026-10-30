"""Deterministic, entirely fictional seed data (same every run so evaluations are repeatable)."""
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import FeeRecord, ScholarshipRecord, StudentProfile, SupportTicket, TicketEvent, User
from app.services.validators import generate_password

# Initial accounts. Passwords are NOT stored in source: they are generated when the database is provisioned and written to login.txt.
ACCOUNTS = [
    {"username": "student_a", "role": "student", "student_id": "S2026001"},
    {"username": "student_b", "role": "student", "student_id": "S2026002"},
    {"username": "student_c", "role": "student", "student_id": "S2026003"},
    {"username": "admin", "role": "admin", "student_id": None},
]

STUDENTS = [
    dict(student_id="S2026001", full_name="Aarav Mehta", programme="PGDM (Big Data Analytics)", trimester="Trimester IV",
         cgpa=3.4, email="aarav.mehta@students.example.edu", phone="+91 90000 10001"),
    dict(student_id="S2026002", full_name="Diya Patel", programme="PGDM (Artificial Intelligence & Data Science)",
         trimester="Trimester IV", cgpa=3.8, email="diya.patel@students.example.edu", phone="+91 90000 10002"),
    dict(student_id="S2026003", full_name="Kabir Shah", programme="PGDM (Big Data Analytics)", trimester="Trimester II",
         cgpa=2.7, email="kabir.shah@students.example.edu", phone="+91 90000 10003"),
]

FEES = [
    dict(student_id="S2026001", term="Trimester IV", total_fee=450000, paid=300000, due_date="2026-11-15", status="Partially paid"),
    dict(student_id="S2026002", term="Trimester IV", total_fee=450000, paid=450000, due_date="2026-11-15", status="Paid"),
    dict(student_id="S2026003", term="Trimester II", total_fee=450000, paid=225000, due_date="2026-10-01", status="Overdue"),
]

SCHOLARSHIPS = [
    dict(student_id="S2026001", name="Merit Scholarship", percent=25, status="Active",
         condition="Trimester CGPA of 3.0 or above", next_review="2026-12-20"),
    dict(student_id="S2026002", name="Merit Scholarship", percent=40, status="Active",
         condition="Trimester CGPA of 3.0 or above", next_review="2026-12-20"),
    dict(student_id="S2026003", name="Need-Based Scholarship", percent=15, status="Under review",
         condition="Family income declaration reviewed by the Scholarship Committee", next_review="2026-11-30"),
]

TICKETS = [
    dict(ticket_ref="TKT-0001", student_id="S2026001", category="course_change", subject="Elective change request",
         description="Requesting a switch from elective E3 to E5.", status="Under Review", idempotency_key="seed-1",
         last_response="We are checking seat availability for E5 and will update you shortly."),
    dict(ticket_ref="TKT-0002", student_id="S2026002", category="academic", subject="Writing workshop booking",
         description="Please book me into the writing-skills workshop.", status="Closed", idempotency_key="seed-2"),
]


def seed_database(passwords: dict | None = None) -> dict:
    """Insert the initial records into an empty database. Returns {username: password}.
    Pass `passwords` to choose them (tests); otherwise a strong random password is generated for every account."""
    passwords = passwords or {}
    creds = {}
    for s in STUDENTS:
        db.session.add(StudentProfile(**s))
    db.session.flush()
    for a in ACCOUNTS:
        pw = passwords.get(a["username"]) or generate_password()
        creds[a["username"]] = pw
        name = next((s["full_name"] for s in STUDENTS if s["student_id"] == a["student_id"]), "Registrar Office")
        db.session.add(User(username=a["username"], password_hash=generate_password_hash(pw), role=a["role"], display_name=name,
                            student_id=a["student_id"], active=True))
    for f in FEES:
        db.session.add(FeeRecord(**f))
    for s in SCHOLARSHIPS:
        db.session.add(ScholarshipRecord(**s))
    for t in TICKETS:
        ticket = SupportTicket(**t)
        db.session.add(ticket)
        db.session.flush()
        db.session.add(TicketEvent(ticket_id=ticket.id, actor_role="student", event_type="created", to_status="Open", note=t["subject"]))
        if t["status"] != "Open":
            db.session.add(TicketEvent(ticket_id=ticket.id, actor_role="admin", event_type="status_changed", from_status="Open",
                                       to_status=t["status"], note=t.get("last_response")))
    db.session.commit()
    return creds


def write_login_file(creds: dict, path) -> None:
    """Write the local credentials file (git-ignored). Roles come from the database so the file always matches it."""
    users = {u.username: u for u in User.query.all()}
    lines = ["LOCAL ACCOUNTS - keep this file private. It is excluded from Git.", "",
             f"{'username':<14}{'role':<10}{'status':<10}password", "-" * 60]
    for name, pw in creds.items():
        u = users[name]
        lines.append(f"{name:<14}{u.role:<10}{'active' if u.active else 'inactive':<10}{pw}")
    lines += ["", "Regenerate any time with:  python scripts/provision_accounts.py --reset-passwords", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
