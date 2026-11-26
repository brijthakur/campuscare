import smtplib
from urllib.parse import urljoin

from flask import current_app, has_request_context, url_for


def create_notification(db, user_id, issue_id, notification_type, message, dedupe_key,
                        send_email, email_link=None):
    cursor = db.execute(
        "INSERT INTO notifications (user_id, issue_id, type, message, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
        (user_id, issue_id, notification_type, message, dedupe_key),
    )
    if cursor.rowcount == 0:
        return None
    user = db.execute(
        "SELECT email, notify_email, is_active FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not user or not user["notify_email"] or not user["is_active"]:
        return cursor.lastrowid
    body = message
    if email_link:
        body += "\n\nOpen CampusCare: " + email_link
    try:
        send_email(user["email"], "CampusCare update", body)
    except (OSError, RuntimeError, smtplib.SMTPException) as error:
        current_app.logger.error(
            "Could not email notification %s to user %s: %s", cursor.lastrowid, user_id, error
        )
    else:
        db.execute("UPDATE notifications SET email_sent = 1 WHERE id = ?", (cursor.lastrowid,))
    return cursor.lastrowid


def issue_link(issue_id):
    if has_request_context():
        return url_for("issue_detail", issue_id=issue_id, _external=True)
    path = f"/issues/{int(issue_id)}"
    base_url = current_app.config.get("PUBLIC_BASE_URL")
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/")) if base_url else path


def notification_counts(db, user_id):
    row = db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) AS unread "
        "FROM notifications WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return {"total": row["total"], "unread": row["unread"] or 0}


def latest_notifications(db, user_id, limit=5):
    return db.execute(
        "SELECT n.*, i.ticket_code FROM notifications n "
        "LEFT JOIN issues i ON i.id = n.issue_id "
        "WHERE n.user_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
