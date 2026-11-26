from datetime import datetime, timedelta, timezone


PRIORITIES = ("Low", "Medium", "High")
PRIORITY_ORDER_SQL = "CASE i.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END"
COMPLETED_SQL = "i.status IN ('Resolved', 'Closed')"
KOLKATA = timezone(timedelta(hours=5, minutes=30), name="IST")
SLA_SETTING_KEYS = {
    "High": "sla_high_hours",
    "Medium": "sla_medium_hours",
    "Low": "sla_low_hours",
}


def parse_utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def settings_value(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise RuntimeError(f"Required SLA setting is missing: {key}")
    try:
        value = int(row["value"])
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"SLA setting {key} must be an integer.") from error
    if value <= 0:
        raise RuntimeError(f"SLA setting {key} must be greater than zero.")
    return value


def calculate_due_at(db, priority, created_at, custom_due_at=None):
    if priority not in PRIORITIES:
        raise ValueError("Invalid issue priority.")
    if custom_due_at:
        return parse_utc(custom_due_at).isoformat(timespec="seconds")
    start = parse_utc(created_at)
    hours = settings_value(db, SLA_SETTING_KEYS[priority])
    return (start + timedelta(hours=hours)).isoformat(timespec="seconds")


def urgency_for(issue, db, now=None):
    if issue["status"] in ("Resolved", "Closed"):
        return "Completed"
    if not issue["due_at"]:
        return "On track"
    now_utc = parse_utc(now or datetime.now(timezone.utc))
    due_at = parse_utc(issue["due_at"])
    if due_at < now_utc:
        return "Overdue"
    if due_at <= now_utc + timedelta(hours=settings_value(db, "due_soon_hours")):
        return "Due soon"
    return "On track"


def issue_order_sql(sort):
    if sort == "newest":
        return "i.created_at DESC, i.id DESC"
    return (
        f"CASE WHEN {COMPLETED_SQL} THEN 1 ELSE 0 END ASC, "
        "CASE WHEN i.due_at IS NULL THEN 1 ELSE 0 END ASC, "
        f"i.due_at ASC, {PRIORITY_ORDER_SQL} ASC, i.created_at ASC, i.id ASC"
    )


def local_datetime_input(value):
    if not value:
        return ""
    return parse_utc(value).astimezone(KOLKATA).strftime("%Y-%m-%dT%H:%M")


def local_datetime_to_utc(value):
    if not value:
        return None
    local = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=KOLKATA)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_datetime_display(value, fmt="%d %b %Y, %I:%M %p"):
    if not value:
        return ""
    return parse_utc(value).astimezone(KOLKATA).strftime(fmt)


def countdown_for(value, now=None):
    if not value:
        return "No deadline"
    seconds = int((parse_utc(value) - parse_utc(now or datetime.now(timezone.utc))).total_seconds())
    if seconds < 0:
        seconds = abs(seconds)
        amount = seconds // 86400
        if amount:
            return f"Overdue by {amount}d"
        amount = seconds // 3600
        if amount:
            return f"Overdue by {amount}h"
        return f"Overdue by {max(1, seconds // 60)}m"
    if seconds < 3600:
        return f"Due in {max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"Due in {seconds // 3600}h"
    return f"Due in {seconds // 86400}d"
