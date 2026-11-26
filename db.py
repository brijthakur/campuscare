import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app, g


_STATUSES = ("Pending", "In Progress", "Resolved", "Closed", "Reopened")
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db


def init_db():
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if existing:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "full_name" not in columns:
            migrate_legacy_schema(db)
            migrate_additive_schema(db)
            return
    execute_schema(db)
    migrate_additive_schema(db)
    db.commit()


def migrate_additive_schema(db):
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "notify_email" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 1 "
            "CHECK (notify_email IN (0, 1))"
        )

    issue_columns = {row["name"] for row in db.execute("PRAGMA table_info(issues)")}
    additions = (
        ("priority", "TEXT NOT NULL DEFAULT 'Medium' CHECK (priority IN ('Low', 'Medium', 'High'))"),
        ("due_at", "TEXT"),
        ("rating", "INTEGER CHECK (rating BETWEEN 1 AND 5)"),
        ("feedback", "TEXT"),
        ("escalated_at", "TEXT"),
    )
    for column, definition in additions:
        if column not in issue_columns:
            db.execute(f"ALTER TABLE issues ADD COLUMN {column} {definition}")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_status_due_at ON issues(status, due_at)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority)")
    defaults = (
        ("sla_high_hours", "24"),
        ("sla_medium_hours", "72"),
        ("sla_low_hours", "168"),
        ("due_soon_hours", "24"),
        ("auto_escalate_hours", "24"),
    )
    db.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", defaults)
    open_issues = db.execute(
        "SELECT id, created_at, priority FROM issues WHERE due_at IS NULL "
        "AND status NOT IN ('Resolved', 'Closed')"
    ).fetchall()
    for issue in open_issues:
        hours_key = {
            "High": "sla_high_hours",
            "Medium": "sla_medium_hours",
            "Low": "sla_low_hours",
        }[issue["priority"]]
        hours = int(db.execute(
            "SELECT value FROM settings WHERE key = ?", (hours_key,)
        ).fetchone()["value"])
        created_at = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        due_at = (created_at + timedelta(hours=hours)).astimezone(timezone.utc).isoformat(timespec="seconds")
        db.execute("UPDATE issues SET due_at = ? WHERE id = ?", (due_at, issue["id"]))
    db.commit()


def execute_schema(db):
    schema_path = Path(__file__).with_name("schema.sql")
    statement = ""
    for line in schema_path.read_text(encoding="utf-8").splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Could not parse schema.sql completely.")


def migrate_legacy_schema(db):
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN")
    for table in ("issue_history", "issues", "users"):
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists:
            db.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
    execute_schema(db)

    users = db.execute("SELECT * FROM legacy_users ORDER BY id").fetchall()
    for user in users:
        db.execute(
            "INSERT INTO users (id, full_name, email, password_hash, role, is_email_verified, "
            "reset_token, reset_token_expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], user["name"], user["email"], user["password_hash"], user["role"],
             user["email_verified"], user["reset_token_hash"], user["reset_expires"],
             user["created_at"], user["created_at"]),
        )

    legacy_issues = db.execute("SELECT * FROM legacy_issues ORDER BY id").fetchall()
    for issue in legacy_issues:
        category = "Cleanliness" if issue["category"] == "Cleaning" else issue["category"]
        ticket_code = f"CC-{issue['id']:06d}-MIGRATED"
        db.execute(
            "INSERT INTO issues (id, ticket_code, title, category, location, description, status, "
            "reporter_id, assigned_staff_id, created_at, updated_at, resolved_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issue["id"], ticket_code, issue["title"], category, issue["location"],
             issue["description"], issue["status"], issue["created_by"], issue["assigned_to"],
             issue["created_at"], issue["updated_at"],
             issue["updated_at"] if issue["status"] in ("Resolved", "Closed") else None,
             issue["updated_at"] if issue["status"] == "Closed" else None),
        )
        old_history = db.execute(
            "SELECT * FROM legacy_issue_history WHERE issue_id = ? ORDER BY id", (issue["id"],)
        ).fetchall()
        old_status = None
        first_history_id = None
        for event in old_history:
            new_status = event["status"] if event["status"] in _STATUSES else issue["status"]
            cursor = db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "remarks, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (issue["id"], event["user_id"] or issue["created_by"], old_status, new_status,
                 event["note"], event["created_at"]),
            )
            if first_history_id is None:
                first_history_id = cursor.lastrowid
            old_status = new_status
        if not old_history:
            cursor = db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "remarks, created_at) VALUES (?, ?, NULL, ?, ?, ?)",
                (issue["id"], issue["created_by"], issue["status"], "Migrated issue history.",
                 issue["created_at"]),
            )
            first_history_id = cursor.lastrowid
        filename = issue["image_filename"]
        if filename:
            filename = Path(filename).name
            filepath = Path(current_app.config["UPLOAD_FOLDER"]) / filename
            if filepath.is_file() and filepath.stat().st_size:
                extension = filepath.suffix.lower()
                mime_type = _IMAGE_MIME_TYPES.get(extension)
                if mime_type:
                    db.execute(
                        "INSERT INTO attachments (issue_id, uploaded_by_id, history_id, "
                        "original_filename, stored_filename, file_path, mime_type, file_size_bytes, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (issue["id"], issue["created_by"], first_history_id, filename, filename,
                         "uploads/" + filename, mime_type, filepath.stat().st_size, issue["created_at"]),
                    )

    db.execute("DROP TABLE legacy_issue_history")
    db.execute("DROP TABLE legacy_issues")
    db.execute("DROP TABLE legacy_users")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
