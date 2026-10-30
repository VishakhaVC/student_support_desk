"""Safe, additive schema upgrade for existing SQLite databases.

Only adds missing columns/tables and fixes legacy status values; it never drops or rewrites existing records.
A timestamped copy of the database file is made before the first change."""
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

from .extensions import db

ADD_COLUMNS = {
    "users": [("active", "BOOLEAN NOT NULL DEFAULT 1"), ("created_at", "DATETIME")],
    "student_profiles": [("status", "VARCHAR(12) NOT NULL DEFAULT 'active'"), ("created_at", "DATETIME")],
    "support_tickets": [("updated_at", "DATETIME"), ("resolved_at", "DATETIME"), ("last_response", "TEXT"),
                        ("source", "VARCHAR(12) NOT NULL DEFAULT 'web'")],
    "approval_requests": [("ticket_id", "INTEGER"), ("execution_result", "VARCHAR(200)")],
}


def pending_columns() -> list[tuple[str, str, str]]:
    insp = inspect(db.engine)
    todo = []
    for table, cols in ADD_COLUMNS.items():
        if not insp.has_table(table):
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        todo += [(table, name, ddl) for name, ddl in cols if name not in have]
    return todo


def backup_database(backup_dir: Path | None = None) -> Path | None:
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    src = Path(url.database)
    if not src.exists():
        return None
    backup_dir = backup_dir or src.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{src.stem}-{datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(src, dest)
    return dest


def upgrade_schema(backup_dir: Path | None = None) -> dict:
    """Create missing tables, add missing columns, normalise legacy data. Returns a summary of what changed."""
    db.create_all()                      # new tables only; existing tables are untouched
    todo = pending_columns()
    summary = {"backup": None, "columns_added": [], "events_backfilled": 0, "statuses_fixed": 0}
    if todo:
        b = backup_database(backup_dir)
        summary["backup"] = str(b) if b else None
        for table, name, ddl in todo:
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            summary["columns_added"].append(f"{table}.{name}")
        db.session.commit()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.session.execute(text("UPDATE users SET created_at = :n WHERE created_at IS NULL"), {"n": now})
    db.session.execute(text("UPDATE student_profiles SET created_at = :n WHERE created_at IS NULL"), {"n": now})
    db.session.execute(text("UPDATE support_tickets SET updated_at = created_at WHERE updated_at IS NULL"))
    r = db.session.execute(text("UPDATE support_tickets SET status = 'Under Review' WHERE status = 'In progress'"))
    summary["statuses_fixed"] = r.rowcount or 0
    r = db.session.execute(text(
        "INSERT INTO ticket_events (ticket_id, actor_user_id, actor_role, event_type, to_status, note, created_at) "
        "SELECT t.id, NULL, 'student', 'created', t.status, t.subject, t.created_at FROM support_tickets t "
        "WHERE NOT EXISTS (SELECT 1 FROM ticket_events e WHERE e.ticket_id = t.id)"))
    summary["events_backfilled"] = r.rowcount or 0
    db.session.commit()
    return summary
