import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    Response,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from db import get_db, init_db
from notifications import create_notification, issue_link, latest_notifications, notification_counts
from sla import (
    PRIORITIES,
    calculate_due_at,
    countdown_for,
    issue_order_sql,
    local_datetime_display,
    local_datetime_input,
    local_datetime_to_utc,
    parse_utc,
    urgency_for,
)


STATUSES = ("Pending", "In Progress", "Resolved", "Closed", "Reopened")
STAFF_STATUSES = ("In Progress", "Resolved")
ISSUE_CATEGORIES = ("Electrical", "Plumbing", "Cleanliness", "Classroom", "Network", "Other")
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
}
PROOF_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=os.environ.get("INSTANCE_PATH"),
    )
    environment = os.environ.get("APP_ENV", "development")
    configured_secret = os.environ.get("SECRET_KEY")
    if environment == "production" and not configured_secret:
        raise RuntimeError("SECRET_KEY must be configured in production.")
    app.config.from_mapping(
        SECRET_KEY=configured_secret or secrets.token_hex(32),
        APP_ENV=environment,
        DATABASE=os.environ.get(
            "DATABASE", os.path.join(app.instance_path, "campuscare.sqlite3")
        ),
        UPLOAD_FOLDER=os.environ.get(
            "UPLOAD_FOLDER", os.path.join(app.instance_path, "uploads")
        ),
        MAX_IMAGE_SIZE=3 * 1024 * 1024,
        MAX_VIDEO_SIZE=50 * 1024 * 1024,
        MAX_CONTENT_LENGTH=54 * 1024 * 1024,
        SHOW_EMAIL_LINKS=environment != "production",
        SMTP_HOST=os.environ.get("SMTP_HOST"),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("SMTP_USERNAME"),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD"),
        SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
        MAIL_FROM=os.environ.get("MAIL_FROM", "CampusCare <noreply@campuscare.local>"),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL"),
        TRUST_PROXY=os.environ.get("TRUST_PROXY", "false").lower() == "true",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=environment == "production",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY must be configured.")
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    os.makedirs(app.instance_path, exist_ok=True)
    database_path = app.config["DATABASE"]
    if database_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(database_path)), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    @app.before_request
    def enforce_csrf():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not expected or not hmac.compare_digest(submitted, expected):
                abort(400, description="The form expired. Reload the page and try again.")

    @app.context_processor
    def inject_template_values():
        user = get_current_user()
        db = get_db() if user else None
        return {
            "current_user": user,
            "csrf_token": get_csrf_token,
            "statuses": STATUSES,
            "categories": ISSUE_CATEGORIES,
            "priorities": PRIORITIES,
            "notification_count": notification_counts(db, user["id"]) if user else {"unread": 0},
            "recent_notifications": latest_notifications(db, user["id"]) if user else [],
            "format_datetime": local_datetime_display,
            "countdown_for": countdown_for,
        }

    @app.errorhandler(400)
    def bad_request(error):
        return render_template(
            "error.html", code=400, title="That request needs a second look",
            message=getattr(error, "description", "Please check the information and try again."),
        ), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html", code=403, title="This area is restricted",
            message="You do not have access to this page or issue.",
        ), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template(
            "error.html", code=404, title="We could not find that page",
            message="The link may be out of date, or the issue may no longer be available.",
        ), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template(
            "error.html", code=413, title="That upload is too large",
            message="Choose an image smaller than 3 MB and try again.",
        ), 413

    @app.errorhandler(500)
    def server_error(_error):
        current_app.logger.exception("Unhandled server error")
        return render_template(
            "error.html", code=500, title="Something went wrong",
            message="Our team has been notified. Please try again in a moment.",
        ), 500

    app.jinja_env.filters["localtime"] = local_datetime_display
    app.jinja_env.filters["datetime_input"] = local_datetime_input
    app.jinja_env.filters["urgency"] = lambda issue: urgency_for(issue, get_db())

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.cli.command("init-db")
    def init_db_command():
        """Create the database tables."""
        init_db()
        print("Database initialized.")

    @app.cli.command("create-admin")
    def create_admin_command():
        """Create an administrator account interactively."""
        import getpass

        name = input("Admin name: ").strip()
        email = input("Admin email: ").strip().lower()
        password = getpass.getpass("Password (8+ characters): ")
        if not name or not EMAIL_PATTERN.match(email) or len(password) < 8:
            raise SystemExit("Provide a name, valid email, and password of at least 8 characters.")
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'admin', 1)",
                (name, email, generate_password_hash(password)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise SystemExit("An account with that email already exists.")
        print("Administrator account created.")

    @app.cli.command("check-deadlines")
    def check_deadlines_command():
        """Create deduplicated due-soon/overdue alerts and escalate stale issues."""
        result = run_deadline_checks()
        print(
            "Deadline check complete: "
            f"{result['due_soon']} due-soon alerts, {result['overdue']} overdue alerts, "
            f"{result['escalated']} escalations."
        )

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Create sample users and issues for a non-production walkthrough."""
        if current_app.config["APP_ENV"] == "production":
            raise SystemExit("Demo accounts are disabled in production.")
        db = get_db()
        demo_users = (
            ("Demo Administrator", "admin@campuscare.demo", "admin"),
            ("Demo Maintenance", "staff@campuscare.demo", "staff"),
            ("Demo Student", "student@campuscare.demo", "student"),
        )
        user_ids = {}
        for name, email, role in demo_users:
            existing_user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing_user:
                user_ids[role] = existing_user["id"]
                continue
            password = secrets.token_urlsafe(12)
            cursor = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, ?, 1)",
                (name, email, generate_password_hash(password), role),
            )
            user_ids[role] = cursor.lastrowid
            print(f"Created {role}: {email} / {password}")
        db.commit()
        now = datetime.now(timezone.utc)
        demo_issues = (
            ("DEMO-OVERDUE", "Classroom projector not powering on", "Classroom", "High",
             now - timedelta(hours=36)),
            ("DEMO-DUE-SOON", "Leaking tap outside the science labs", "Plumbing", "Medium",
             now + timedelta(hours=8)),
            ("DEMO-ON-TRACK", "Unstable Wi-Fi in the north library", "Network", "Low",
             now + timedelta(days=5)),
        )
        for code, title, category, priority, due_at in demo_issues:
            issue = db.execute("SELECT id FROM issues WHERE ticket_code = ?", (code,)).fetchone()
            if issue:
                continue
            created_at = now - timedelta(days=2)
            cursor = db.execute(
                "INSERT INTO issues (ticket_code, title, category, location, description, status, "
                "reporter_id, assigned_staff_id, priority, due_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'In Progress', ?, ?, ?, ?, ?, ?)",
                (code, title, category, "Main campus", "Sample issue for demonstrating the workflow.",
                 user_ids["student"], user_ids["staff"], priority,
                 due_at.isoformat(timespec="seconds"), created_at.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")),
            )
            db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "previous_staff_id, new_staff_id, remarks, created_at) "
                "VALUES (?, ?, 'Pending', 'In Progress', NULL, ?, 'Demo issue assigned to maintenance.', ?)",
                (cursor.lastrowid, user_ids["admin"], user_ids["staff"],
                 now.isoformat(timespec="seconds")),
            )
        db.commit()
        print("Demo data is ready. Existing demo users were left unchanged.")

    @app.route("/")
    def index():
        if get_current_user():
            return redirect(url_for("dashboard"))
        return render_template("index.html")

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            department = request.form.get("department", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or len(name) > 100 or len(department) > 100:
                flash("Enter your name (up to 100 characters).", "danger")
            elif not EMAIL_PATTERN.match(email) or len(email) > 254:
                flash("Enter a valid email address.", "danger")
            elif len(password) < 8:
                flash("Your password must be at least 8 characters.", "danger")
            else:
                db = get_db()
                try:
                    db.execute(
                        "INSERT INTO users (full_name, email, password_hash, role, department, "
                        "verification_token) VALUES (?, ?, ?, 'student', ?, NULL)",
                        (name, email, generate_password_hash(password), department or None),
                    )
                    db.commit()
                    flash("Registration complete. You can sign in now.", "success")
                    return redirect(url_for("login"))
                except sqlite3.IntegrityError:
                    flash("An account with that email already exists.", "danger")
        return render_template("register.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not user or not check_password_hash(user["password_hash"], password):
                flash("Email or password is incorrect.", "danger")
            elif not user["is_active"]:
                flash("This account is inactive. Contact an administrator.", "danger")
            else:
                session.clear()
                session["user_id"] = user["id"]
                get_csrf_token()
                return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.route("/logout", methods=("POST",))
    def logout():
        session.clear()
        flash("You have been signed out.", "info")
        return redirect(url_for("index"))

    @app.route("/forgot-password", methods=("GET", "POST"))
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            user = get_db().execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                get_db().execute(
                    "UPDATE users SET reset_token = ?, reset_token_expires_at = ?, updated_at = ? WHERE id = ?",
                    (hash_token(token), expires, utc_now(), user["id"]),
                )
                get_db().commit()
                reset_url = url_for("reset_password", token=token, _external=True)
                send_email(user["email"], "Reset your CampusCare password",
                           "Use this link within one hour to reset your password: " + reset_url)
                if current_app.config["SHOW_EMAIL_LINKS"]:
                    flash(reset_url, "password-reset-link")
            flash("If an account exists for that email, password-reset instructions have been sent.", "success")
        return render_template("forgot_password.html")

    @app.route("/reset-password/<token>", methods=("GET", "POST"))
    def reset_password(token):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = get_db().execute(
            "SELECT id, reset_token_expires_at FROM users WHERE reset_token = ? AND is_active = 1",
            (token_hash,)
        ).fetchone()
        if not user or not user["reset_token_expires_at"]:
            flash("That password-reset link is invalid or has expired.", "danger")
            return redirect(url_for("forgot_password"))
        try:
            expires = datetime.fromisoformat(user["reset_token_expires_at"])
        except ValueError:
            expires = datetime.min.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            flash("That password-reset link is invalid or has expired.", "danger")
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < 8:
                flash("Your password must be at least 8 characters.", "danger")
            else:
                db = get_db()
                db.execute(
                    "UPDATE users SET password_hash = ?, reset_token = NULL, "
                    "reset_token_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (generate_password_hash(password), utc_now(), user["id"]),
                )
                db.commit()
                flash("Password changed. You can now sign in.", "success")
                return redirect(url_for("login"))
        return render_template("reset_password.html")

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = get_current_user()
        db = get_db()
        filters = {
            "q": request.args.get("q", "").strip()[:120],
            "status": request.args.get("status", ""),
            "category": request.args.get("category", ""),
            "priority": request.args.get("priority", ""),
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
            "sort": request.args.get("sort", "deadline"),
        }
        if filters["status"] and filters["status"] not in STATUSES:
            abort(400, description="Invalid status filter.")
        if filters["category"] and filters["category"] not in ISSUE_CATEGORIES:
            abort(400, description="Invalid category filter.")
        if filters["priority"] and filters["priority"] not in PRIORITIES:
            abort(400, description="Invalid priority filter.")
        if filters["sort"] not in ("deadline", "newest"):
            abort(400, description="Invalid sort order.")
        for date_key in ("date_from", "date_to"):
            if filters[date_key]:
                try:
                    datetime.strptime(filters[date_key], "%Y-%m-%d")
                except ValueError:
                    abort(400, description=f"Invalid {date_key.replace('_', ' ')} filter.")
        if filters["date_from"] and filters["date_to"] \
                and filters["date_from"] > filters["date_to"]:
            abort(400, description="The start date must not be after the end date.")

        where = []
        params = []
        if user["role"] == "staff":
            where.append("i.assigned_staff_id = ?")
            params.append(user["id"])
        elif user["role"] == "student":
            where.append("i.reporter_id = ?")
            params.append(user["id"])
        for key, column in (
            ("status", "i.status"),
            ("category", "i.category"),
            ("priority", "i.priority"),
        ):
            if filters[key]:
                where.append(f"{column} = ?")
                params.append(filters[key])
        if filters["q"]:
            where.append(
                "(i.ticket_code LIKE ? OR i.title LIKE ? OR COALESCE(i.location, '') LIKE ?)"
            )
            term = "%" + filters["q"] + "%"
            params.extend((term, term, term))
        if filters["date_from"]:
            where.append("substr(i.created_at, 1, 10) >= ?")
            params.append(filters["date_from"])
        if filters["date_to"]:
            where.append("substr(i.created_at, 1, 10) <= ?")
            params.append(filters["date_to"])
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        issues = db.execute(
            "SELECT i.*, creator.full_name AS reporter, assignee.full_name AS assignee "
            "FROM issues i JOIN users creator ON creator.id = i.reporter_id "
            "LEFT JOIN users assignee ON assignee.id = i.assigned_staff_id"
            + where_sql + f" ORDER BY {issue_order_sql(filters['sort'])}",
            params,
        ).fetchall()
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(hours=_setting_int(db, "due_soon_hours"))).isoformat(timespec="seconds")
        now_iso = now.isoformat(timespec="seconds")
        if user["role"] == "admin":
            counts = {row["status"]: row["total"] for row in db.execute(
                "SELECT status, COUNT(*) AS total FROM issues GROUP BY status"
            ).fetchall()}
            counts.update({
                "Total": db.execute("SELECT COUNT(*) FROM issues").fetchone()[0],
                "Overdue": db.execute(
                    "SELECT COUNT(*) FROM issues WHERE status NOT IN ('Resolved','Closed') "
                    "AND due_at < ?", (now_iso,),
                ).fetchone()[0],
                "Due soon": db.execute(
                    "SELECT COUNT(*) FROM issues WHERE status NOT IN ('Resolved','Closed') "
                    "AND due_at >= ? AND due_at <= ?", (now_iso, soon),
                ).fetchone()[0],
                "Unassigned": db.execute(
                    "SELECT COUNT(*) FROM issues WHERE assigned_staff_id IS NULL "
                    "AND status NOT IN ('Resolved','Closed')"
                ).fetchone()[0],
            })
            resolved_stats = db.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN resolved_at <= due_at THEN 1 ELSE 0 END) "
                "AS on_time FROM issues WHERE resolved_at IS NOT NULL"
            ).fetchone()
            counts["On time %"] = round(
                100 * (resolved_stats["on_time"] or 0) / resolved_stats["total"], 1
            ) if resolved_stats["total"] else 0
            counts["Average rating"] = db.execute(
                "SELECT ROUND(AVG(rating), 1) FROM issues WHERE rating IS NOT NULL"
            ).fetchone()[0] or 0
            staff = db.execute(
                "SELECT id, full_name AS name, email, department FROM users "
                "WHERE role = 'staff' AND is_active = 1 ORDER BY full_name"
            ).fetchall()
            staff_roster = db.execute(
                "SELECT u.id, u.full_name AS name, u.email, u.department, u.is_active, "
                "COUNT(i.id) AS active_work "
                "FROM users u LEFT JOIN issues i ON i.assigned_staff_id = u.id "
                "AND i.status NOT IN ('Resolved', 'Closed') "
                "WHERE u.role = 'staff' GROUP BY u.id ORDER BY u.is_active DESC, u.full_name"
            ).fetchall()
            counts["Total"] = counts.get("Total", 0)
            chart_data = admin_chart_data(db)
        elif user["role"] == "staff":
            counts = {"Total": len(issues), "Overdue": sum(
                1 for issue in issues if urgency_for(issue, db) == "Overdue"
            )}
            staff = []
            staff_roster = []
            chart_data = {}
        else:
            student_total = db.execute(
                "SELECT COUNT(*) FROM issues WHERE reporter_id = ?", (user["id"],)
            ).fetchone()[0]
            student_resolved = db.execute(
                "SELECT COUNT(*) FROM issues WHERE reporter_id = ? AND status IN ('Resolved','Closed')",
                (user["id"],),
            ).fetchone()[0]
            counts = {
                "Total": student_total,
                "Open": student_total - student_resolved,
                "Resolved": student_resolved,
            }
            staff = []
            staff_roster = []
            chart_data = {}
        return render_template(
            "dashboard.html", issues=issues, counts=counts, staff=staff,
            staff_roster=staff_roster, chart_data=chart_data, filters=filters
        )

    @app.route("/notifications")
    @login_required
    def notifications_page():
        items = get_db().execute(
            "SELECT n.*, i.ticket_code FROM notifications n "
            "LEFT JOIN issues i ON i.id = n.issue_id "
            "WHERE n.user_id = ? ORDER BY n.created_at DESC, n.id DESC",
            (get_current_user()["id"],),
        ).fetchall()
        return render_template("notifications.html", notifications=items)

    @app.route("/notifications/<int:notification_id>/read", methods=("POST",))
    @login_required
    def mark_notification_read(notification_id):
        db = get_db()
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, get_current_user()["id"]),
        )
        db.commit()
        return redirect(url_for("notifications_page"))

    @app.route("/notifications/read-all", methods=("POST",))
    @login_required
    def mark_all_notifications_read():
        db = get_db()
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
            (get_current_user()["id"],),
        )
        db.commit()
        flash("All notifications marked as read.", "success")
        return redirect(url_for("notifications_page"))

    @app.route("/settings/notifications", methods=("POST",))
    @login_required
    def update_notification_preferences():
        enabled = 1 if request.form.get("notify_email") == "on" else 0
        db = get_db()
        db.execute(
            "UPDATE users SET notify_email = ?, updated_at = ? WHERE id = ?",
            (enabled, utc_now(), get_current_user()["id"]),
        )
        db.commit()
        flash("Email notification preferences saved.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/issues/new", methods=("GET", "POST"))
    @roles_required("student")
    def new_issue():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            category = request.form.get("category", "").strip()
            description = request.form.get("description", "").strip()
            location = request.form.get("location", "").strip()
            if not title or len(title) > 150 or category not in ISSUE_CATEGORIES \
                    or not description or len(description) > 5000 or len(location) > 150:
                flash("Check the issue details and try again.", "danger")
                return render_template("new_issue.html")
            image = request.files.get("image")
            attachment = None
            if image and image.filename:
                try:
                    attachment = save_uploaded_image(image)
                except OSError:
                    current_app.logger.exception("Could not save an issue image")
                    flash("The image could not be saved. Please try again.", "danger")
                    return render_template("new_issue.html")
                if attachment is None:
                    flash("Upload a valid JPG, PNG, or WebP image under 3 MB.", "danger")
                    return render_template("new_issue.html")
            db = get_db()
            now = utc_now()
            ticket_code = f"CC-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4).upper()}"
            due_at = calculate_due_at(db, "Medium", now)
            cursor = db.execute(
                "INSERT INTO issues (ticket_code, title, category, description, location, "
                "reporter_id, status, priority, due_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Pending', 'Medium', ?, ?, ?)",
                (ticket_code, title, category, description, location or None,
                 get_current_user()["id"], due_at, now, now),
            )
            issue_id = cursor.lastrowid
            db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "remarks, created_at) VALUES (?, ?, NULL, 'Pending', 'Issue reported.', ?)",
                (issue_id, get_current_user()["id"], now),
            )
            history_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if attachment:
                insert_attachments(
                    db, issue_id, get_current_user()["id"], history_id, [attachment], now
                )
            notify_admins(
                db, issue_id, "new_issue",
                f"New issue {ticket_code} was reported and needs review.",
                f"new-issue:{issue_id}",
            )
            db.commit()
            flash("Your issue has been submitted.", "success")
            return redirect(url_for("issue_detail", issue_id=issue_id))
        return render_template("new_issue.html")

    @app.route("/issues/<int:issue_id>")
    @login_required
    def issue_detail(issue_id):
        issue = get_issue(issue_id)
        ensure_issue_access(issue)
        history = get_db().execute(
            "SELECT h.*, u.full_name AS author, assigned.full_name AS new_staff "
            "FROM issue_history h "
            "LEFT JOIN users u ON u.id = h.changed_by_id "
            "LEFT JOIN users assigned ON assigned.id = h.new_staff_id "
            "WHERE h.issue_id = ? "
            "ORDER BY h.id",
            (issue_id,),
        ).fetchall()
        attachments = get_db().execute(
            "SELECT * FROM attachments WHERE issue_id = ? ORDER BY id", (issue_id,)
        ).fetchall()
        staff = []
        if get_current_user()["role"] == "admin":
            staff = get_db().execute(
                "SELECT id, full_name AS name, department FROM users "
                "WHERE role = 'staff' AND is_active = 1 ORDER BY full_name"
            ).fetchall()
        can_reopen = (
            get_current_user()["role"] == "student"
            and issue["reporter_id"] == get_current_user()["id"]
            and issue["status"] in ("Resolved", "Closed")
        )
        return render_template(
            "issue_detail.html", issue=issue, history=history, staff=staff,
            attachments=attachments, can_reopen=can_reopen
        )

    @app.route("/issues/<int:issue_id>/admin-update", methods=("POST",))
    @roles_required("admin")
    def admin_update_issue(issue_id):
        issue = get_issue(issue_id)
        status = request.form.get("status", "")
        assignee = request.form.get("assigned_to", "").strip()
        note = request.form.get("note", "").strip()
        priority = request.form.get("priority", issue["priority"])
        custom_due_input = request.form.get("due_at", "").strip()
        if status not in STATUSES or priority not in PRIORITIES or len(note) > 1000:
            abort(400, description="Invalid status or note.")
        if status == "Closed" and issue["status"] not in ("Resolved", "Closed"):
            abort(400, description="An issue must be resolved before it can be closed.")
        if status == "Reopened" and issue["status"] != "Reopened":
            abort(400, description="Students must use the Not solved action to reopen an issue.")
        assigned_to = None
        if assignee:
            try:
                assigned_to = int(assignee)
            except ValueError:
                abort(400, description="Invalid staff member.")
            if not get_db().execute(
                "SELECT id FROM users WHERE id = ? AND role = 'staff' AND is_active = 1",
                (assigned_to,),
            ).fetchone():
                abort(400, description="Invalid staff member.")
        db = get_db()
        now = utc_now()
        previous_status = issue["status"]
        previous_staff_id = issue["assigned_staff_id"]
        previous_priority = issue["priority"]
        try:
            custom_due_at = (
                local_datetime_to_utc(custom_due_input)
                if request.form.get("custom_due_enabled") == "on" else None
            )
        except ValueError:
            abort(400, description="Invalid deadline.")
        if request.form.get("custom_due_enabled") == "on" and not custom_due_at:
            abort(400, description="Choose a custom deadline.")
        if custom_due_at:
            due_at = custom_due_at
        elif priority != previous_priority or not issue["due_at"]:
            due_at = calculate_due_at(db, priority, issue["created_at"])
        else:
            due_at = issue["due_at"]
        proof_attachments = []
        if status == "Resolved" and previous_status != "Resolved":
            uploads, error = prepare_resolution_proof()
            if error:
                flash(error, "danger")
                return redirect(url_for("issue_detail", issue_id=issue_id))
            try:
                proof_attachments = save_uploaded_attachments(uploads)
            except OSError:
                current_app.logger.exception("Could not save resolution proof for issue %s", issue_id)
                flash("The proof files could not be saved. No issue changes were made.", "danger")
                return redirect(url_for("issue_detail", issue_id=issue_id))
        db.execute(
            "UPDATE issues SET status = ?, assigned_staff_id = ?, priority = ?, due_at = ?, updated_at = ?, "
            "resolved_at = CASE WHEN ? = 'Resolved' THEN COALESCE(resolved_at, ?) "
            "WHEN ? = 'Closed' AND resolved_at IS NULL THEN ? "
            "WHEN ? = 'Reopened' THEN NULL ELSE resolved_at END, "
            "closed_at = CASE WHEN ? = 'Closed' THEN COALESCE(closed_at, ?) "
            "ELSE NULL END WHERE id = ?",
            (status, assigned_to, priority, due_at, now,
             status, now, status, now, status, status, now, issue["id"]),
        )
        details = []
        if issue["status"] != status:
            details.append("Status changed to " + status + ".")
        if previous_staff_id != assigned_to:
            details.append("Assignment updated.")
        if previous_priority != priority:
            details.append(f"Priority changed to {priority}.")
        if issue["due_at"] != due_at:
            details.append("Deadline updated.")
        if note:
            details.append(note)
        history_cursor = db.execute(
            "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
            "previous_staff_id, new_staff_id, remarks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (issue["id"], get_current_user()["id"], previous_status, status, previous_staff_id,
             assigned_to, " ".join(details) or "Issue reviewed.", now),
        )
        history_id = history_cursor.lastrowid
        actor_id = get_current_user()["id"]
        if proof_attachments:
            insert_attachments(
                db, issue_id, actor_id, history_id, proof_attachments, now
            )
        if previous_status != status:
            if status in ("Resolved", "Closed"):
                message = f"Your issue {issue['ticket_code']} has been solved."
                if note:
                    message += " Resolution: " + note
                solved_link = issue_link(issue_id) + "#rate-fix"
                notify_user(
                    db, issue["reporter_id"], issue_id, "solved", message,
                    f"solved:{issue_id}:{history_id}", solved_link,
                )
            notify_user(
                db, issue["reporter_id"], issue_id, "status",
                f"Issue {issue['ticket_code']} status changed to {status}.",
                f"status:{issue_id}:{history_id}", issue_link(issue_id),
            )
        if previous_staff_id != assigned_to and assigned_to:
            assigned_message = (
                f"New task {issue['ticket_code']} assigned, due "
                f"{local_datetime_display(due_at)}."
            )
            notify_user(
                db, assigned_to, issue_id, "assigned", assigned_message,
                f"assigned:{issue_id}:{history_id}:staff", issue_link(issue_id),
            )
            notify_user(
                db, issue["reporter_id"], issue_id, "assigned",
                f"Staff has been assigned to issue {issue['ticket_code']}.",
                f"assigned:{issue_id}:{history_id}:reporter", issue_link(issue_id),
            )
        if issue["due_at"] != due_at and assigned_to:
            notify_user(
                db, assigned_to, issue_id, "deadline",
                f"Deadline for issue {issue['ticket_code']} changed to {local_datetime_display(due_at)}.",
                f"deadline:{issue_id}:{history_id}", issue_link(issue_id),
            )
        if note:
            notify_other_participants(
                db, issue, actor_id, issue_id, "remark",
                f"New update on issue {issue['ticket_code']}: {note}",
                f"remark:{issue_id}:{history_id}", issue_link(issue_id),
                assigned_staff_id=assigned_to,
            )
        db.commit()
        flash("Issue updated.", "success")
        return redirect(url_for("issue_detail", issue_id=issue_id))

    @app.route("/issues/<int:issue_id>/staff-update", methods=("POST",))
    @roles_required("staff")
    def staff_update_issue(issue_id):
        issue = get_issue(issue_id)
        if issue["assigned_staff_id"] != get_current_user()["id"]:
            abort(403)
        if issue["status"] == "Closed":
            abort(400, description="A closed issue must be reopened by an administrator before staff can update it.")
        status = request.form.get("status", "")
        note = request.form.get("note", "").strip()
        if status not in STAFF_STATUSES or len(note) > 1000:
            abort(400, description="Invalid status or note.")
        proof_attachments = []
        if status == "Resolved" and issue["status"] != "Resolved":
            uploads, error = prepare_resolution_proof()
            if error:
                flash(error, "danger")
                return redirect(url_for("issue_detail", issue_id=issue_id))
            try:
                proof_attachments = save_uploaded_attachments(uploads)
            except OSError:
                current_app.logger.exception("Could not save resolution proof for issue %s", issue_id)
                flash("The proof files could not be saved. No issue changes were made.", "danger")
                return redirect(url_for("issue_detail", issue_id=issue_id))
        now = utc_now()
        db = get_db()
        previous_status = issue["status"]
        db.execute(
            "UPDATE issues SET status = ?, updated_at = ?, "
            "resolved_at = CASE WHEN ? = 'Resolved' THEN COALESCE(resolved_at, ?) ELSE NULL END, "
            "closed_at = NULL WHERE id = ?",
            (status, now, status, now, issue["id"]),
        )
        history_cursor = db.execute(
            "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
            "previous_staff_id, new_staff_id, remarks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (issue["id"], get_current_user()["id"], previous_status, status,
             issue["assigned_staff_id"], issue["assigned_staff_id"],
             note or "Status changed to " + status + ".", now),
        )
        history_id = history_cursor.lastrowid
        actor_id = get_current_user()["id"]
        if proof_attachments:
            insert_attachments(
                db, issue_id, actor_id, history_id, proof_attachments, now
            )
        remarks = note or "Status changed to " + status + "."
        if previous_status != status:
            if status == "Resolved":
                solved_message = (
                    f"Your issue {issue['ticket_code']} has been solved. "
                    f"Resolution: {remarks}"
                )
                notify_user(
                    db, issue["reporter_id"], issue_id, "solved", solved_message,
                    f"solved:{issue_id}:{history_id}", issue_link(issue_id) + "#rate-fix",
                )
                notify_admins(
                    db, issue_id, "status",
                    f"Issue {issue['ticket_code']} is resolved and ready for review.",
                    f"ready-review:{issue_id}:{history_id}",
                )
            notify_user(
                db, issue["reporter_id"], issue_id, "status",
                f"Issue {issue['ticket_code']} status changed to {status}.",
                f"status:{issue_id}:{history_id}", issue_link(issue_id),
            )
        if note:
            notify_other_participants(
                db, issue, actor_id, issue_id, "remark",
                f"New update on issue {issue['ticket_code']}: {note}",
                f"remark:{issue_id}:{history_id}", issue_link(issue_id),
                assigned_staff_id=issue["assigned_staff_id"],
            )
        db.commit()
        flash("Progress updated.", "success")
        return redirect(url_for("issue_detail", issue_id=issue_id))

    @app.route("/issues/<int:issue_id>/remark", methods=("POST",))
    @login_required
    def add_issue_remark(issue_id):
        issue = get_issue(issue_id)
        ensure_issue_access(issue)
        note = request.form.get("note", "").strip()
        if not note or len(note) > 1000:
            abort(400, description="A comment of 1,000 characters or fewer is required.")
        now = utc_now()
        db = get_db()
        cursor = db.execute(
            "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
            "previous_staff_id, new_staff_id, remarks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (issue_id, get_current_user()["id"], issue["status"], issue["status"],
             issue["assigned_staff_id"], issue["assigned_staff_id"], note, now),
        )
        history_id = cursor.lastrowid
        notify_other_participants(
            db, issue, get_current_user()["id"], issue_id, "remark",
            f"New update on issue {issue['ticket_code']}: {note}",
            f"remark:{issue_id}:{history_id}", issue_link(issue_id),
        )
        db.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue_id))
        db.commit()
        flash("Your update has been added.", "success")
        return redirect(url_for("issue_detail", issue_id=issue_id))

    @app.route("/admin/staff", methods=("POST",))
    @roles_required("admin")
    def create_staff():
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        password = request.form.get("password", "")
        if not name or len(name) > 100 or len(department) > 100 \
                or not EMAIL_PATTERN.match(email) or len(password) < 8:
            flash("Enter a name, valid email, and password of at least 8 characters.", "danger")
        else:
            try:
                get_db().execute(
                    "INSERT INTO users (full_name, email, password_hash, role, department, "
                    "is_email_verified) VALUES (?, ?, ?, 'staff', ?, 1)",
                    (name, email, generate_password_hash(password), department or None),
                )
                get_db().commit()
                flash("Staff account created.", "success")
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "danger")
        return redirect(url_for("dashboard"))

    @app.route("/admin/staff/<int:staff_id>/toggle-active", methods=("POST",))
    @roles_required("admin")
    def toggle_staff_active(staff_id):
        db = get_db()
        staff_member = db.execute(
            "SELECT id, full_name, is_active FROM users WHERE id = ? AND role = 'staff'",
            (staff_id,),
        ).fetchone()
        if not staff_member:
            abort(404)
        activating = not staff_member["is_active"]
        now = utc_now()
        if not activating:
            assigned_issues = db.execute(
                "SELECT * FROM issues WHERE assigned_staff_id = ? "
                "AND status NOT IN ('Resolved', 'Closed')",
                (staff_id,),
            ).fetchall()
            for issue in assigned_issues:
                db.execute(
                    "UPDATE issues SET assigned_staff_id = NULL, updated_at = ? WHERE id = ?",
                    (now, issue["id"]),
                )
                cursor = db.execute(
                    "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                    "previous_staff_id, new_staff_id, remarks, created_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (issue["id"], get_current_user()["id"], issue["status"], issue["status"],
                     staff_id, "Staff account deactivated; issue returned to the unassigned queue.", now),
                )
                notify_user(
                    db, issue["reporter_id"], issue["id"], "assigned",
                    f"Your issue {issue['ticket_code']} is being reassigned.",
                    f"staff-deactivated:{staff_id}:{cursor.lastrowid}:reporter",
                    issue_link(issue["id"]),
                )
                notify_admins(
                    db, issue["id"], "assigned",
                    f"Issue {issue['ticket_code']} is unassigned and needs a new owner.",
                    f"staff-deactivated:{staff_id}:{cursor.lastrowid}:admin",
                    issue_link(issue["id"]),
                )
        db.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if activating else 0, now, staff_id),
        )
        db.commit()
        flash(
            f"{staff_member['full_name']} was {'activated' if activating else 'deactivated'}.",
            "success",
        )
        return redirect(url_for("dashboard"))

    @app.route("/admin/settings", methods=("GET", "POST"))
    @roles_required("admin")
    def admin_settings():
        db = get_db()
        keys = (
            "sla_high_hours", "sla_medium_hours", "sla_low_hours",
            "due_soon_hours", "auto_escalate_hours",
        )
        if request.method == "POST":
            values = {}
            for key in keys:
                raw = request.form.get(key, "")
                try:
                    value = int(raw)
                except ValueError:
                    abort(400, description=f"{key.replace('_', ' ').title()} must be a whole number.")
                if not 1 <= value <= 8760:
                    abort(400, description="SLA values must be between 1 and 8,760 hours.")
                values[key] = str(value)
            db.executemany("UPDATE settings SET value = ? WHERE key = ?",
                           [(value, key) for key, value in values.items()])
            db.commit()
            flash("Service deadlines and alert settings were saved.", "success")
            return redirect(url_for("admin_settings"))
        settings = {row["key"]: row["value"] for row in db.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?, ?)", keys
        ).fetchall()}
        return render_template("admin_settings.html", settings=settings)

    @app.route("/admin/issues.csv")
    @roles_required("admin")
    def export_issues():
        rows = get_db().execute(
            "SELECT i.ticket_code, i.title, i.category, i.location, i.status, i.priority, "
            "i.created_at, i.due_at, i.resolved_at, i.closed_at, i.rating, i.feedback, "
            "reporter.full_name AS reporter, staff.full_name AS assigned_staff "
            "FROM issues i JOIN users reporter ON reporter.id = i.reporter_id "
            "LEFT JOIN users staff ON staff.id = i.assigned_staff_id "
            "ORDER BY i.created_at DESC, i.id DESC"
        ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow((
            "Ticket", "Title", "Category", "Location", "Status", "Priority", "Reporter",
            "Assigned staff", "Created", "Due", "Resolved", "Closed", "Rating", "Feedback",
        ))
        for row in rows:
            writer.writerow([
                _csv_safe(row[key]) for key in (
                    "ticket_code", "title", "category", "location", "status", "priority",
                    "reporter", "assigned_staff", "created_at", "due_at", "resolved_at",
                    "closed_at", "rating", "feedback",
                )
            ])
        return Response(
            output.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=campuscare-issues.csv"},
        )

    @app.route("/issues/<int:issue_id>/rate", methods=("POST",))
    @roles_required("student")
    def rate_issue(issue_id):
        issue = get_issue(issue_id)
        if issue["reporter_id"] != get_current_user()["id"]:
            abort(403)
        if issue["status"] not in ("Resolved", "Closed"):
            abort(400, description="You can rate an issue after it has been resolved.")
        if issue["rating"] is not None:
            abort(400, description="You have already rated this resolution.")
        try:
            rating = int(request.form.get("rating", ""))
        except ValueError:
            abort(400, description="Choose a rating from 1 to 5.")
        feedback = request.form.get("feedback", "").strip()
        if not 1 <= rating <= 5 or len(feedback) > 1000:
            abort(400, description="Choose a rating from 1 to 5 and keep feedback under 1,000 characters.")
        db = get_db()
        db.execute(
            "UPDATE issues SET rating = ?, feedback = ?, updated_at = ? WHERE id = ?",
            (rating, feedback or None, utc_now(), issue_id),
        )
        cursor = db.execute(
            "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
            "remarks, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, get_current_user()["id"], issue["status"], issue["status"],
             f"Student rated the resolution {rating}/5." + (f" Feedback: {feedback}" if feedback else ""),
             utc_now()),
        )
        notify_admins(
            db, issue_id, "remark",
            f"Issue {issue['ticket_code']} received a {rating}/5 rating.",
            f"rating:{issue_id}:{cursor.lastrowid}", issue_link(issue_id),
        )
        db.commit()
        flash("Thanks for helping us improve campus maintenance.", "success")
        return redirect(url_for("issue_detail", issue_id=issue_id) + "#rate-fix")

    @app.route("/issues/<int:issue_id>/reopen", methods=("POST",))
    @roles_required("student")
    def reopen_issue(issue_id):
        issue = get_issue(issue_id)
        if issue["reporter_id"] != get_current_user()["id"]:
            abort(403)
        if issue["status"] not in ("Resolved", "Closed"):
            abort(400, description="Only resolved issues can be reopened.")
        reason = request.form.get("reason", "").strip()
        if not reason or len(reason) > 1000:
            flash("Please explain why the issue is not solved (up to 1000 characters).", "danger")
            return redirect(url_for("issue_detail", issue_id=issue_id))
        db = get_db()
        now = utc_now()
        due_at = calculate_due_at(db, issue["priority"], now)
        db.execute(
            "UPDATE issues SET status = 'Reopened', due_at = ?, resolved_at = NULL, closed_at = NULL, "
            "rating = NULL, feedback = NULL, updated_at = ? WHERE id = ?",
            (due_at, now, issue_id),
        )
        remarks = "Student reported the issue was not solved: " + reason
        if issue["rating"]:
            remarks += f" Previous rating: {issue['rating']}/5."
        if issue["feedback"]:
            remarks += f" Previous feedback: {issue['feedback']}"
        cursor = db.execute(
            "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
            "previous_staff_id, new_staff_id, remarks, created_at) "
            "VALUES (?, ?, ?, 'Reopened', ?, ?, ?, ?)",
            (issue_id, get_current_user()["id"], issue["status"], issue["assigned_staff_id"],
             issue["assigned_staff_id"], remarks, now),
        )
        notify_admins(
            db, issue_id, "status",
            f"Issue {issue['ticket_code']} was reopened by the student: {reason}",
            f"reopened:{issue_id}:{cursor.lastrowid}", issue_link(issue_id),
        )
        if issue["assigned_staff_id"]:
            notify_user(
                db, issue["assigned_staff_id"], issue_id, "status",
                f"Issue {issue['ticket_code']} was reopened and needs further work: {reason}",
                f"reopened:{issue_id}:{cursor.lastrowid}:staff", issue_link(issue_id),
            )
        db.commit()
        flash("The issue has been reopened for your campus team.", "success")
        return redirect(url_for("issue_detail", issue_id=issue_id))

    @app.route("/uploads/<path:filename>")
    @login_required
    def uploaded_file(filename):
        issue = get_db().execute(
            "SELECT i.reporter_id, i.assigned_staff_id, a.mime_type FROM issues i "
            "JOIN attachments a ON a.issue_id = i.id WHERE a.stored_filename = ?",
            (filename,),
        ).fetchone()
        if not issue:
            abort(404)
        user = get_current_user()
        if user["role"] not in ("admin", "staff") and issue["reporter_id"] != user["id"]:
            abort(403)
        if user["role"] == "staff" and issue["assigned_staff_id"] != user["id"]:
            abort(403)
        response = send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)
        response.mimetype = issue["mime_type"]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; media-src 'self'"
        )
        return response

    with app.app_context():
        init_db()

    return app


def get_current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    user = get_db().execute(
        "SELECT id, full_name AS name, email, role, is_email_verified AS email_verified, "
        "department, is_active, notify_email FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user and not user["is_active"]:
        session.clear()
        return None
    return user


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if get_current_user()["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def send_email(recipient, subject, body):
    host = current_app.config["SMTP_HOST"]
    if not host:
        if not current_app.config["SHOW_EMAIL_LINKS"]:
            raise RuntimeError("SMTP_HOST must be configured when development email links are disabled.")
        current_app.logger.info("Development email to %s: %s\n%s", recipient, subject, body)
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = current_app.config["MAIL_FROM"]
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP(host, current_app.config["SMTP_PORT"], timeout=10) as smtp:
        if current_app.config["SMTP_USE_TLS"]:
            smtp.starttls()
        username = current_app.config["SMTP_USERNAME"]
        if username:
            smtp.login(username, current_app.config["SMTP_PASSWORD"] or "")
        smtp.send_message(message)


def validate_uploaded_attachment(upload, kind, max_size):
    original_filename = secure_filename(upload.filename or "")
    extension = Path(original_filename).suffix.lower()
    if not original_filename or extension not in PROOF_MIME_TYPES:
        return None
    if kind == "image" and extension not in IMAGE_MIME_TYPES:
        return None
    if kind == "video" and not PROOF_MIME_TYPES[extension].startswith("video/"):
        return None

    data = upload.read(max_size + 1)
    upload.seek(0)
    if not data or len(data) > max_size:
        return None

    if extension in IMAGE_MIME_TYPES:
        valid_signature = any(
            data.startswith(signature) for signature in IMAGE_SIGNATURES[extension]
        )
        if extension == ".webp":
            valid_signature = valid_signature and len(data) >= 12 and data[8:12] == b"WEBP"
    elif extension == ".webm":
        valid_signature = data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:4096]
    elif extension == ".mov":
        valid_signature = len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] == b"qt  "
    else:
        valid_signature = (
            len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] != b"qt  "
        )
    if not valid_signature:
        return None
    return {
        "file": upload,
        "extension": extension,
        "original_filename": original_filename,
        "mime_type": PROOF_MIME_TYPES[extension],
        "file_size_bytes": len(data),
    }


def save_uploaded_attachments(attachments):
    saved = []
    paths = []
    try:
        for attachment in attachments:
            filename = secrets.token_hex(16) + attachment["extension"]
            path = Path(current_app.config["UPLOAD_FOLDER"]) / filename
            paths.append(path)
            attachment["file"].save(str(path))
            saved.append({
                key: value for key, value in attachment.items() if key != "file"
            } | {"stored_filename": filename})
    except OSError:
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                current_app.logger.exception("Could not remove a partial upload %s", path)
        raise
    return saved


def save_uploaded_image(image):
    attachment = validate_uploaded_attachment(
        image, "image", current_app.config["MAX_IMAGE_SIZE"]
    )
    if attachment is None:
        return None
    return save_uploaded_attachments([attachment])[0]


def prepare_resolution_proof():
    photo = request.files.get("proof_photo")
    video = request.files.get("proof_video")
    if not photo or not photo.filename or not video or not video.filename:
        return None, "Upload both a new photo and a new video to mark this issue resolved."
    photo_attachment = validate_uploaded_attachment(
        photo, "image", current_app.config["MAX_IMAGE_SIZE"]
    )
    if photo_attachment is None:
        return None, "Upload a valid JPG, PNG, or WebP proof photo under 3 MB."
    video_attachment = validate_uploaded_attachment(
        video, "video", current_app.config["MAX_VIDEO_SIZE"]
    )
    if video_attachment is None:
        return None, "Upload a valid MP4, WebM, or MOV proof video under 50 MB."
    return [photo_attachment, video_attachment], None


def insert_attachments(db, issue_id, uploader_id, history_id, attachments, created_at):
    for attachment in attachments:
        db.execute(
            "INSERT INTO attachments (issue_id, uploaded_by_id, history_id, original_filename, "
            "stored_filename, file_path, mime_type, file_size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issue_id, uploader_id, history_id, attachment["original_filename"],
             attachment["stored_filename"], "uploads/" + attachment["stored_filename"],
             attachment["mime_type"], attachment["file_size_bytes"], created_at),
        )


def get_issue(issue_id):
    issue = get_db().execute(
        "SELECT i.*, creator.full_name AS reporter, creator.email AS reporter_email, "
        "assignee.full_name AS assignee, assignee.email AS assignee_email "
        "FROM issues i JOIN users creator ON creator.id = i.reporter_id "
        "LEFT JOIN users assignee ON assignee.id = i.assigned_staff_id WHERE i.id = ?",
        (issue_id,),
    ).fetchone()
    if not issue:
        abort(404)
    return issue


def ensure_issue_access(issue):
    user = get_current_user()
    if user["role"] == "admin":
        return
    if user["role"] == "staff" and issue["assigned_staff_id"] == user["id"]:
        return
    if user["role"] == "student" and issue["reporter_id"] == user["id"]:
        return
    abort(403)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def notify_user(db, user_id, issue_id, notification_type, message, dedupe_key, link=None):
    return create_notification(
        db, user_id, issue_id, notification_type, message, dedupe_key,
        send_email, email_link=link,
    )


def notify_admins(db, issue_id, notification_type, message, event_key, link=None):
    admins = db.execute(
        "SELECT id FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchall()
    for admin in admins:
        notify_user(
            db, admin["id"], issue_id, notification_type, message,
            f"{event_key}:admin:{admin['id']}", link or issue_link(issue_id),
        )


def notify_other_participants(db, issue, actor_id, issue_id, notification_type,
                              message, event_key, link, assigned_staff_id=None):
    recipients = {issue["reporter_id"]}
    staff_id = assigned_staff_id if assigned_staff_id is not None else issue["assigned_staff_id"]
    if staff_id:
        recipients.add(staff_id)
    admins = db.execute(
        "SELECT id FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchall()
    recipients.update(admin["id"] for admin in admins)
    for user_id in recipients - {actor_id}:
        notify_user(
            db, user_id, issue_id, notification_type, message,
            f"{event_key}:user:{user_id}", link,
        )


def run_deadline_checks():
    db = get_db()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    due_soon_hours = _setting_int(db, "due_soon_hours")
    auto_escalate_hours = _setting_int(db, "auto_escalate_hours")
    issues = db.execute(
        "SELECT * FROM issues WHERE status NOT IN ('Resolved', 'Closed') AND due_at IS NOT NULL"
    ).fetchall()
    stats = {"due_soon": 0, "overdue": 0, "escalated": 0}
    for issue in issues:
        deadline = _as_utc(issue["due_at"])
        if deadline < now:
            alert_type = "overdue"
            alert_message = f"Issue {issue['ticket_code']} is overdue."
            stats["overdue"] += 1
        elif deadline <= now + timedelta(hours=due_soon_hours):
            alert_type = "due_soon"
            alert_message = f"Issue {issue['ticket_code']} is due soon."
            stats["due_soon"] += 1
        else:
            alert_type = None
            alert_message = ""
        if alert_type:
            recipients = set()
            if issue["assigned_staff_id"]:
                recipients.add(issue["assigned_staff_id"])
            admins = db.execute(
                "SELECT id FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchall()
            recipients.update(admin["id"] for admin in admins)
            for user_id in recipients:
                notify_user(
                    db, user_id, issue["id"], alert_type, alert_message,
                    f"{alert_type}:{issue['id']}:{issue['due_at']}:{user_id}",
                    issue_link(issue["id"]),
                )
        if deadline + timedelta(hours=auto_escalate_hours) < now and not issue["escalated_at"]:
            cursor = db.execute(
                "UPDATE issues SET priority = 'High', escalated_at = ?, updated_at = ? "
                "WHERE id = ? AND escalated_at IS NULL",
                (now_iso, now_iso, issue["id"]),
            )
            if cursor.rowcount:
                actor = db.execute(
                    "SELECT id FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                actor_id = actor["id"] if actor else issue["reporter_id"]
                db.execute(
                    "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                    "remarks, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (issue["id"], actor_id, issue["status"], issue["status"],
                     "Automatically escalated to High priority after exceeding the configured "
                     "overdue period.", now_iso),
                )
                notify_admins(
                    db, issue["id"], "escalated",
                    f"Issue {issue['ticket_code']} was escalated to High priority.",
                    f"escalated:{issue['id']}", issue_link(issue["id"]),
                )
                stats["escalated"] += 1
    db.commit()
    return stats


def _setting_int(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise RuntimeError(f"Required setting is missing: {key}")
    try:
        value = int(row["value"])
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Setting {key} must be an integer.") from error
    if value <= 0:
        raise RuntimeError(f"Setting {key} must be greater than zero.")
    return value


def _as_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _csv_safe(value):
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def admin_chart_data(db):
    categories = db.execute(
        "SELECT category, COUNT(*) AS total FROM issues GROUP BY category ORDER BY category"
    ).fetchall()
    status_counts = {
        row["status"]: row["total"] for row in db.execute(
            "SELECT status, COUNT(*) AS total FROM issues GROUP BY status"
        ).fetchall()
    }
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=29)
    daily = {
        row["day"]: row["total"] for row in db.execute(
            "SELECT date(created_at) AS day, COUNT(*) AS total FROM issues "
            "WHERE date(created_at) >= ? GROUP BY date(created_at)",
            (start.isoformat(),),
        ).fetchall()
    }
    labels = [(start + timedelta(days=index)) for index in range(30)]
    return {
        "category_labels": [row["category"] for row in categories],
        "category_values": [row["total"] for row in categories],
        "status_labels": list(STATUSES),
        "status_values": [status_counts.get(status, 0) for status in STATUSES],
        "trend_labels": [day.strftime("%d %b") for day in labels],
        "trend_values": [daily.get(day.isoformat(), 0) for day in labels],
    }


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("APP_ENV", "development") != "production",
    )
