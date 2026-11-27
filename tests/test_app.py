import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app


class CampusCareTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(root / "test.sqlite3"),
            "UPLOAD_FOLDER": str(root / "uploads"),
            "SHOW_EMAIL_LINKS": True,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        response = self.client.get("/login")
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def register_user(self, name, email, password="password123"):
        token = self.csrf()
        response = self.client.post("/register", data={
            "csrf_token": token, "name": name, "email": email, "password": password,
        }, follow_redirects=True)
        self.assertIn(b"Registration complete. You can sign in now.", response.data)

    def login(self, email, password="password123"):
        response = self.client.get("/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        return self.client.post("/login", data={
            "csrf_token": token, "email": email, "password": password,
        }, follow_redirects=True)

    def test_student_issue_history_and_authorization(self):
        self.register_user("A Student", "student@example.test")
        response = self.login("student@example.test")
        self.assertIn(b"Issue dashboard", response.data)
        response = self.client.get("/issues/new")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/new", data={
            "csrf_token": token, "title": "Broken light", "category": "Electrical",
            "description": "Light is not working.", "location": "Block A, room 10",
        }, follow_redirects=True)
        self.assertIn(b"Issue history", response.data)
        self.assertIn(b"Issue reported.", response.data)
        self.client.post("/logout", data={"csrf_token": self.csrf()})

        self.register_user("Another Student", "other@example.test")
        self.login("other@example.test")
        self.assertEqual(self.client.get("/issues/1").status_code, 403)

    def test_admin_assignment_staff_update_and_admin_closure(self):
        with self.app.app_context():
            from app import get_db
            from werkzeug.security import generate_password_hash
            db = get_db()
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'admin', 1)",
                ("Admin", "admin@example.test", generate_password_hash("adminpass123")),
            )
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'staff', 1)",
                ("Staff", "staff@example.test", generate_password_hash("staffpass123")),
            )
            db.commit()
            staff_id = db.execute(
                "SELECT id FROM users WHERE email = 'staff@example.test'"
            ).fetchone()["id"]
        self.register_user("A Student", "student@example.test")
        self.login("student@example.test")
        response = self.client.get("/issues/new")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        self.client.post("/issues/new", data={
            "csrf_token": token, "title": "Leaking tap", "category": "Plumbing",
            "description": "Water is leaking.", "location": "East wing",
        })
        self.client.post("/logout", data={"csrf_token": self.csrf()})

        self.login("admin@example.test", "adminpass123")
        response = self.client.get("/issues/1")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": token, "assigned_to": str(staff_id), "status": "In Progress",
            "note": "Assigned for repair.",
        }, follow_redirects=True)
        self.assertIn(b"Assigned for repair.", response.data)
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": token, "assigned_to": str(staff_id), "status": "Closed",
            "note": "Attempt to close before resolution.",
        })
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'new_issue' AND user_id = "
                "(SELECT id FROM users WHERE role = 'admin')"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'assigned'"
            ).fetchone()[0], 2)
        self.client.post("/logout", data={"csrf_token": self.csrf()})

        self.login("staff@example.test", "staffpass123")
        response = self.client.get("/issues/1")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": token, "status": "Resolved", "note": "Replaced the tap.",
        }, follow_redirects=True)
        self.assertIn(b"Upload both a new photo and a new video", response.data)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            self.assertEqual(db.execute(
                "SELECT status FROM issues WHERE id = 1"
            ).fetchone()["status"], "In Progress")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM attachments WHERE issue_id = 1"
            ).fetchone()[0], 0)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        photo_only = {"proof_photo": (self._image_file(self._valid_png()), "repair.png")}
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": token, "status": "Resolved", "note": "Replaced the tap.",
            **photo_only,
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Upload both a new photo and a new video", response.data)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        invalid_proof = self._proof_uploads()
        invalid_proof["proof_video"] = (self._image_file(b"not a video"), "repair.mp4")
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": token, "status": "Resolved", "note": "Replaced the tap.",
            **invalid_proof,
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"valid MP4, WebM, or MOV proof video", response.data)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            self.assertEqual(db.execute(
                "SELECT status FROM issues WHERE id = 1"
            ).fetchone()["status"], "In Progress")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM attachments WHERE issue_id = 1"
            ).fetchone()[0], 0)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": token, "status": "Resolved", "note": "Replaced the tap.",
            **self._proof_uploads(),
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Replaced the tap.", response.data)
        video_match = re.search(rb'<source src="([^"]+)" type="video/mp4">', response.data)
        self.assertIsNotNone(video_match, response.data.decode())
        video_response = self.client.get(video_match.group(1).decode())
        self.assertEqual(video_response.mimetype, "video/mp4")
        self.assertIn("media-src 'self'", video_response.headers["Content-Security-Policy"])
        video_response.close()
        with self.app.app_context():
            from app import get_db
            db = get_db()
            proof_rows = db.execute(
                "SELECT a.history_id, h.new_status FROM attachments a "
                "JOIN issue_history h ON h.id = a.history_id WHERE a.issue_id = 1"
            ).fetchall()
            self.assertEqual(len(proof_rows), 2)
            self.assertEqual({row["new_status"] for row in proof_rows}, {"Resolved"})
            self.assertEqual(len({row["history_id"] for row in proof_rows}), 1)
            student_id = db.execute(
                "SELECT id FROM users WHERE email = 'student@example.test'"
            ).fetchone()["id"]
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = 'solved'",
                (student_id,),
            ).fetchone()[0], 1)
            self.assertGreaterEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'remark'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'status' AND message LIKE '%In Progress%'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'status' AND message LIKE '%ready for review%'"
            ).fetchone()[0], 1)
            solved_message = db.execute(
                "SELECT message FROM notifications WHERE type = 'solved' "
                "AND user_id = ?",
                (student_id,),
            ).fetchone()["message"]
            self.assertIn("Replaced the tap.", solved_message)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.register_user("Another Student", "another@example.test")
        self.login("another@example.test")
        self.assertEqual(self.client.get(video_match.group(1).decode()).status_code, 403)
        self.client.post("/logout", data={"csrf_token": self.csrf()})

        self.login("admin@example.test", "adminpass123")
        response = self.client.get("/issues/1")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": token, "assigned_to": str(staff_id), "status": "Closed",
            "note": "Closure confirmed.",
        }, follow_redirects=True)
        self.assertIn(b"Closed", response.data)
        self.assertIn(b"Closure confirmed.", response.data)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.login("staff@example.test", "staffpass123")
        response = self.client.get("/issues/1")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        self.assertIn(b"An administrator must reopen", response.data)
        self.assertEqual(self.client.post("/issues/1/staff-update", data={
            "csrf_token": token, "status": "In Progress", "note": "Attempt after closure.",
        }).status_code, 400)

    def test_new_and_existing_unverified_users_can_sign_in_and_csrf_is_required(self):
        from unittest.mock import patch

        token = self.csrf()
        with patch("app.send_email") as send_email:
            response = self.client.post("/register", data={
                "csrf_token": token, "name": "New Student", "email": "new@example.test",
                "password": "password123",
            })
        self.assertEqual(response.status_code, 302)
        send_email.assert_not_called()
        self.assertIn(b"Issue dashboard", self.login("new@example.test").data)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            registered_user = db.execute(
                "SELECT is_email_verified, verification_token FROM users WHERE email = ?",
                ("new@example.test",),
            ).fetchone()
            self.assertEqual(registered_user["is_email_verified"], 0)
            self.assertIsNone(registered_user["verification_token"])
            from werkzeug.security import generate_password_hash
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'student', 0)",
                ("Existing Student", "existing@example.test",
                 generate_password_hash("password123")),
            )
            db.commit()
        self.assertIn(b"Issue dashboard", self.login("existing@example.test").data)
        self.assertEqual(self.client.post("/login", data={"email": "a", "password": "b"}).status_code, 400)

    def test_password_reset_link_is_single_use(self):
        self.register_user("A Student", "student@example.test")
        response = self.client.get("/forgot-password")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/forgot-password", data={
            "csrf_token": token, "email": "student@example.test",
        }, follow_redirects=True)
        match = re.search(rb'class="alert-link" href="(https?://[^"]+)"', response.data)
        self.assertIsNotNone(match, response.data.decode())
        reset_path = re.sub(rb"^https?://[^/]+", b"", match.group(1)).decode()
        response = self.client.get(reset_path)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        self.client.post(reset_path, data={"csrf_token": token, "password": "newpassword456"})
        self.assertEqual(self.client.get(reset_path).status_code, 302)
        self.assertIn(b"Issue dashboard", self.login("student@example.test", "newpassword456").data)

    def test_upload_requires_image_signature_and_owner_access(self):
        self.register_user("A Student", "student@example.test")
        self.login("student@example.test")
        response = self.client.get("/issues/new")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/new", data={
            "csrf_token": token, "title": "Photo report", "category": "Other",
            "description": "Image attached.",
            "image": (self._image_file(b"not an image"), "photo.png"),
        }, content_type="multipart/form-data")
        self.assertIn(b"Upload a valid", response.data)

        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "08060000001f15c4890000000b49444154789c636000020000"
            "050001a5f645400000000049454e44ae426082"
        )
        response = self.client.get("/issues/new")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/new", data={
            "csrf_token": token, "title": "Photo report", "category": "Other",
            "description": "Image attached.",
            "image": (self._image_file(png), "photo.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        detail = self.client.get("/issues/1")
        image_match = re.search(rb'<img class="issue-photo[^"]*" src="([^"]+)"', detail.data)
        self.assertIsNotNone(image_match, detail.data.decode())
        image_path = image_match.group(1).decode()
        image_response = self.client.get(image_path)
        self.assertEqual(image_response.mimetype, "image/png")
        image_response.close()

    def test_image_upload_limit_is_three_megabytes(self):
        self.register_user("A Student", "size@example.test")
        self.login("size@example.test")
        response = self.client.get("/issues/new")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        oversized_png = b"\x89PNG\r\n\x1a\n" + b"x" * (3 * 1024 * 1024)
        response = self.client.post("/issues/new", data={
            "csrf_token": csrf, "title": "Large photo", "category": "Other",
            "description": "This upload is over the limit.",
            "image": (self._image_file(oversized_png), "large.png"),
        }, content_type="multipart/form-data")
        self.assertIn(b"under 3 MB", response.data)
        response.close()

    def test_proof_upload_validation_accepts_supported_media_signatures(self):
        from io import BytesIO
        from werkzeug.datastructures import FileStorage
        from app import validate_uploaded_attachment

        supported = (
            ("repair.jpg", b"\xff\xd8\xff" + b"jpeg", "image"),
            ("repair.png", self._valid_png(), "image"),
            ("repair.webp", b"RIFF\x00\x00\x00\x00WEBP", "image"),
            ("repair.mp4", b"\x00\x00\x00\x10ftypisom0000", "video"),
            ("repair.webm", b"\x1a\x45\xdf\xa3" + b"\x00webm", "video"),
            ("repair.mov", b"\x00\x00\x00\x10ftypqt  0000", "video"),
        )
        with self.app.app_context():
            for filename, content, kind in supported:
                upload = FileStorage(stream=BytesIO(content), filename=filename)
                self.assertIsNotNone(
                    validate_uploaded_attachment(upload, kind, 1024)
                )
            mismatched = FileStorage(
                stream=BytesIO(b"not an image"), filename="fake.jpg"
            )
            self.assertIsNone(
                validate_uploaded_attachment(mismatched, "image", 1024)
            )

    def test_inactive_accounts_cannot_sign_in(self):
        with self.app.app_context():
            from app import get_db
            from werkzeug.security import generate_password_hash
            get_db().execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified, is_active) "
                "VALUES (?, ?, ?, 'student', 1, 0)",
                ("Inactive Student", "inactive@example.test", generate_password_hash("password123")),
            )
            get_db().commit()
        response = self.login("inactive@example.test")
        self.assertIn(b"This account is inactive.", response.data)

    def test_legacy_database_migrates_without_losing_issue_and_attachment_data(self):
        root = Path(self.temp_dir.name)
        database = root / "legacy.sqlite3"
        upload_folder = root / "uploads"
        upload_folder.mkdir(exist_ok=True)
        image = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "08060000001f15c4890000000b49444154789c636000020000"
            "050001a5f645400000000049454e44ae426082"
        )
        (upload_folder / "legacy.png").write_bytes(image)
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL, email_verified INTEGER NOT NULL,
                reset_token_hash TEXT, reset_expires TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE issues (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT NOT NULL, location TEXT NOT NULL, image_filename TEXT,
                created_by INTEGER NOT NULL, assigned_to INTEGER, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE issue_history (
                id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, user_id INTEGER,
                status TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO users VALUES
                (1, 'Reporter', 'reporter@example.test', 'hash', 'student', 1, NULL, NULL, '2026-01-01T00:00:00+00:00');
            INSERT INTO issues VALUES
                (7, 'Old leak', 'Cleaning', 'A leaking sink.', 'Hall 2', 'legacy.png',
                 1, NULL, 'Resolved', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00');
            INSERT INTO issues VALUES
                (8, 'Old open issue', 'Network', 'Wi-Fi access point is offline.', 'Library',
                 NULL, 1, NULL, 'Pending', '2026-01-01T00:00:00+00:00',
                 '2026-01-01T00:00:00+00:00');
            INSERT INTO issue_history VALUES
                (1, 7, 1, 'Pending', 'Issue reported.', '2026-01-01T00:00:00+00:00'),
                (2, 7, 1, 'Resolved', 'Fixed.', '2026-01-02T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()

        migrated_app = create_app({
            "TESTING": True,
            "SECRET_KEY": "migration-test-secret",
            "DATABASE": str(database),
            "UPLOAD_FOLDER": str(upload_folder),
        })
        with migrated_app.app_context():
            from app import get_db
            db = get_db()
            issue = db.execute("SELECT * FROM issues WHERE id = 7").fetchone()
            self.assertEqual(issue["category"], "Cleanliness")
            self.assertEqual(issue["ticket_code"], "CC-000007-MIGRATED")
            self.assertEqual(issue["resolved_at"], issue["updated_at"])
            open_issue = db.execute("SELECT due_at FROM issues WHERE id = 8").fetchone()
            self.assertEqual(open_issue["due_at"], "2026-01-04T00:00:00+00:00")
            self.assertEqual(db.execute(
                "SELECT notify_email FROM users WHERE id = 1"
            ).fetchone()["notify_email"], 1)
            settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM settings")}
            self.assertEqual(settings["sla_high_hours"], "24")
            self.assertEqual(settings["sla_medium_hours"], "72")
            history = db.execute(
                "SELECT old_status, new_status, remarks FROM issue_history "
                "WHERE issue_id = 7 ORDER BY id"
            ).fetchall()
            self.assertEqual([(row["old_status"], row["new_status"]) for row in history],
                             [(None, "Pending"), ("Pending", "Resolved")])
            self.assertEqual(history[1]["remarks"], "Fixed.")
            attachment = db.execute("SELECT * FROM attachments WHERE issue_id = 7").fetchone()
            self.assertEqual(attachment["stored_filename"], "legacy.png")
            self.assertEqual(attachment["file_size_bytes"], len(image))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertNotIn("legacy_users", tables)
            db.execute("UPDATE settings SET value = '96' WHERE key = 'sla_medium_hours'")
            db.commit()
            from app import init_db
            init_db()
            self.assertEqual(db.execute(
                "SELECT value FROM settings WHERE key = 'sla_medium_hours'"
            ).fetchone()["value"], "96")
            self.assertEqual(db.execute(
                "SELECT due_at FROM issues WHERE id = 8"
            ).fetchone()["due_at"], "2026-01-04T00:00:00+00:00")

    def test_sla_deadlines_follow_configured_priority_hours(self):
        from app import get_db
        from sla import calculate_due_at
        with self.app.app_context():
            db = get_db()
            db.executemany(
                "UPDATE settings SET value = ? WHERE key = ?",
                [("6", "sla_high_hours"), ("30", "sla_medium_hours"), ("120", "sla_low_hours")],
            )
            created_at = "2026-01-01T00:00:00+00:00"
            self.assertEqual(calculate_due_at(db, "High", created_at), "2026-01-01T06:00:00+00:00")
            self.assertEqual(calculate_due_at(db, "Medium", created_at), "2026-01-02T06:00:00+00:00")
            self.assertEqual(calculate_due_at(db, "Low", created_at), "2026-01-06T00:00:00+00:00")
            with self.assertRaises(ValueError):
                calculate_due_at(db, "Urgent", created_at)

    def test_urgency_and_deadline_ordering(self):
        from datetime import datetime, timezone
        from app import get_db
        from sla import issue_order_sql, urgency_for
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            now = datetime(2026, 1, 10, tzinfo=timezone.utc)
            self.assertEqual(urgency_for({
                "status": "Pending", "due_at": "2026-01-09T00:00:00+00:00",
            }, db, now), "Overdue")
            self.assertEqual(urgency_for({
                "status": "Pending", "due_at": "2026-01-10T12:00:00+00:00",
            }, db, now), "Due soon")
            self.assertEqual(urgency_for({
                "status": "In Progress", "due_at": "2026-01-15T00:00:00+00:00",
            }, db, now), "On track")
            self.assertEqual(urgency_for({
                "status": "Resolved", "due_at": "2026-01-09T00:00:00+00:00",
            }, db, now), "Completed")
            self.assertIn("i.due_at ASC", issue_order_sql("deadline"))
            self.assertIn("i.created_at DESC", issue_order_sql("newest"))
            user_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'student', 1)",
                ("Sort Student", "sort@example.test", generate_password_hash("password123")),
            ).lastrowid
            deadline_issues = (
                ("overdue", "2026-01-09T00:00:00+00:00", "Low", "Pending", "2026-01-01"),
                ("due-soon", "2026-01-10T12:00:00+00:00", "Medium", "Pending", "2026-01-02"),
                ("on-track", "2026-01-15T00:00:00+00:00", "High", "In Progress", "2026-01-03"),
                ("completed", "2026-01-08T00:00:00+00:00", "High", "Resolved", "2026-01-04"),
            )
            for suffix, due_at, priority, status, created in deadline_issues:
                db.execute(
                    "INSERT INTO issues (ticket_code, title, category, description, reporter_id, "
                    "status, priority, due_at, created_at, updated_at) "
                    "VALUES (?, ?, 'Other', 'Sort test', ?, ?, ?, ?, ?, ?)",
                    ("SORT-" + suffix, suffix, user_id, status, priority, due_at,
                     created + "T00:00:00+00:00", created + "T00:00:00+00:00"),
                )
            ordered = db.execute(
                "SELECT ticket_code FROM issues i WHERE reporter_id = ? "
                f"ORDER BY {issue_order_sql('deadline')}",
                (user_id,),
            ).fetchall()
            self.assertEqual(
                [row["ticket_code"] for row in ordered],
                ["SORT-overdue", "SORT-due-soon", "SORT-on-track", "SORT-completed"],
            )

    def test_deadline_checker_creates_deduplicated_alerts_and_escalates_once(self):
        from datetime import datetime, timedelta, timezone
        from app import get_db, run_deadline_checks
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            users = []
            for name, email, role in (
                ("Admin", "admin@example.test", "admin"),
                ("Staff", "staff@example.test", "staff"),
                ("Student", "student@example.test", "student"),
            ):
                cursor = db.execute(
                    "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (name, email, generate_password_hash("password123"), role),
                )
                users.append(cursor.lastrowid)
            admin_id, staff_id, student_id = users
            now = datetime.now(timezone.utc)
            issues = (
                ("Overdue escalation", now - timedelta(hours=48), "High"),
                ("Overdue alert", now - timedelta(hours=3), "Medium"),
                ("Due soon alert", now + timedelta(hours=2), "Low"),
            )
            issue_ids = []
            for index, (title, due_at, priority) in enumerate(issues):
                cursor = db.execute(
                    "INSERT INTO issues (ticket_code, title, category, description, reporter_id, "
                    "assigned_staff_id, status, priority, due_at, created_at, updated_at) "
                    "VALUES (?, ?, 'Other', 'Test issue', ?, ?, 'Pending', ?, ?, ?, ?)",
                    (f"CC-TEST-{index}", title, student_id, staff_id, priority,
                     due_at.isoformat(), (now - timedelta(days=5)).isoformat(), now.isoformat()),
                )
                issue_ids.append(cursor.lastrowid)
            db.commit()
            first = run_deadline_checks()
            self.assertEqual(first["overdue"], 2)
            self.assertEqual(first["due_soon"], 1)
            self.assertEqual(first["escalated"], 1)
            before = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            second = run_deadline_checks()
            after = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            self.assertEqual(second["escalated"], 0)
            self.assertEqual(before, after)
            self.assertEqual(db.execute(
                "SELECT priority FROM issues WHERE id = ?", (issue_ids[0],)
            ).fetchone()["priority"], "High")
            self.assertIsNotNone(db.execute(
                "SELECT escalated_at FROM issues WHERE id = ?", (issue_ids[0],)
            ).fetchone()["escalated_at"])
            self.assertGreaterEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = 'overdue'",
                (admin_id,),
            ).fetchone()[0], 2)
            self.assertGreaterEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = 'due_soon'",
                (staff_id,),
            ).fetchone()[0], 1)
            self.assertIsNotNone(db.execute(
                "SELECT id FROM users WHERE id = ?", (student_id,)
            ).fetchone())

    def test_notification_dedupe_does_not_send_duplicate_email(self):
        from app import get_db
        from notifications import create_notification
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'student', 1)",
                ("Student", "dedupe@example.test", generate_password_hash("password123")),
            )
            db.commit()
            sent = []

            def capture_email(recipient, subject, body):
                sent.append((recipient, subject, body))

            args = (db, cursor.lastrowid, None, "status", "Status updated.", "unique-event")
            create_notification(*args, capture_email)
            create_notification(*args, capture_email)
            self.assertEqual(len(sent), 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE dedupe_key = 'unique-event'"
            ).fetchone()[0], 1)
            db.execute("UPDATE users SET notify_email = 0 WHERE id = ?", (cursor.lastrowid,))
            db.commit()
            create_notification(
                db, cursor.lastrowid, None, "status", "No email requested.",
                "email-preference-off", capture_email,
            )
            self.assertEqual(len(sent), 1)

    def test_student_can_reopen_closed_issue_with_reason_and_new_proof(self):
        from datetime import datetime, timezone

        self.register_user("A Student", "student@example.test")
        with self.app.app_context():
            from app import get_db
            from werkzeug.security import generate_password_hash
            db = get_db()
            admin_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'admin', 1)",
                ("Admin", "admin@example.test", generate_password_hash("adminpass123")),
            ).lastrowid
            staff_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'staff', 1)",
                ("Staff", "staff@example.test", generate_password_hash("staffpass123")),
            ).lastrowid
            db.commit()
        self.login("student@example.test")
        response = self.client.get("/issues/new")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        self.client.post("/issues/new", data={
            "csrf_token": csrf, "title": "Loose classroom desk", "category": "Classroom",
            "description": "The desk is unstable.",
        })
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.login("admin@example.test", "adminpass123")
        response = self.client.get("/issues/1")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Closed",
            "priority": "Medium", "note": "Attempt to close before resolving.",
        })
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Resolved",
            "priority": "Medium", "note": "The desk has been repaired.",
        }, follow_redirects=True)
        self.assertIn(b"Upload both a new photo and a new video", response.data)
        with self.app.app_context():
            from app import get_db
            self.assertEqual(get_db().execute(
                "SELECT status FROM issues WHERE id = 1"
            ).fetchone()["status"], "Pending")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Resolved",
            "priority": "Medium", "note": "The desk has been repaired.",
            **self._proof_uploads(),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        csrf = self.csrf()
        response = self.client.post("/issues/1/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Closed",
            "priority": "Medium", "note": "Resolution confirmed.",
        })
        self.assertEqual(response.status_code, 302)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.login("student@example.test")
        detail = self.client.get("/issues/1")
        self.assertIn(b"Rate the fix", detail.data)
        self.assertIn(b"Not solved", detail.data)
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        response = self.client.post("/issues/1/rate", data={
            "csrf_token": csrf, "rating": "5", "feedback": "Quick and helpful.",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            self.assertEqual(get_db().execute(
                "SELECT rating, feedback FROM issues WHERE id = 1"
            ).fetchone()["rating"], 5)
            get_db().execute(
                "UPDATE issues SET due_at = '2020-01-01T00:00:00+00:00' WHERE id = 1"
            )
            get_db().commit()
        csrf = self.csrf()
        response = self.client.post("/issues/1/reopen", data={
            "csrf_token": csrf, "reason": "",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            self.assertEqual(get_db().execute(
                "SELECT status FROM issues WHERE id = 1"
            ).fetchone()["status"], "Closed")
        csrf = self.csrf()
        response = self.client.post("/issues/1/reopen", data={
            "csrf_token": csrf, "reason": "The desk still wobbles when used.",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            row = get_db().execute(
                "SELECT status, rating, feedback, due_at, resolved_at, closed_at "
                "FROM issues WHERE id = 1"
            ).fetchone()
            self.assertEqual(row["status"], "Reopened")
            self.assertIsNone(row["rating"])
            self.assertIsNone(row["feedback"])
            self.assertIsNone(row["resolved_at"])
            self.assertIsNone(row["closed_at"])
            self.assertGreater(
                datetime.fromisoformat(row["due_at"]),
                datetime.now(timezone.utc),
            )
            history = get_db().execute(
                "SELECT remarks FROM issue_history WHERE issue_id = 1 "
                "AND new_status = 'Reopened'"
            ).fetchone()
            self.assertIn("The desk still wobbles when used.", history["remarks"])
            self.assertEqual(get_db().execute(
                "SELECT COUNT(*) FROM issue_history WHERE issue_id = 1 AND new_status = 'Reopened'"
            ).fetchone()[0], 1)
            self.assertNotEqual(row["due_at"], "2020-01-01T00:00:00+00:00")
            for user_id in (staff_id, admin_id):
                notification = get_db().execute(
                    "SELECT message FROM notifications WHERE issue_id = 1 AND user_id = ? "
                    "AND type = 'status' AND message LIKE '%reopened%'",
                    (user_id,),
                ).fetchone()
                self.assertIsNotNone(notification)
                self.assertIn("The desk still wobbles when used.", notification["message"])
            self.assertEqual(get_db().execute(
                "SELECT COUNT(*) FROM attachments WHERE issue_id = 1"
            ).fetchone()[0], 2)

        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.login("staff@example.test", "staffpass123")
        detail = self.client.get("/issues/1")
        self.assertEqual(detail.status_code, 200, detail.data.decode())
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": csrf, "status": "Resolved", "note": "Retried the repair.",
        }, follow_redirects=True)
        self.assertIn(b"Upload both a new photo and a new video", response.data)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            self.assertEqual(db.execute(
                "SELECT status FROM issues WHERE id = 1"
            ).fetchone()["status"], "Reopened")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM attachments WHERE issue_id = 1"
            ).fetchone()[0], 2)
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
        response = self.client.post("/issues/1/staff-update", data={
            "csrf_token": csrf, "status": "Resolved", "note": "Repaired the desk again.",
            **self._proof_uploads(),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            proof_rows = db.execute(
                "SELECT a.history_id, h.new_status FROM attachments a "
                "JOIN issue_history h ON h.id = a.history_id WHERE a.issue_id = 1"
            ).fetchall()
            self.assertEqual(len(proof_rows), 4)
            self.assertEqual(
                sum(row["new_status"] == "Resolved" for row in proof_rows), 4
            )
            self.assertEqual(len({row["history_id"] for row in proof_rows}), 2)

    def test_admin_priority_recalculates_deadline_and_custom_deadline_is_respected(self):
        from datetime import datetime, timedelta, timezone
        from app import get_db
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'admin', 1)",
                ("Admin", "admin@example.test", generate_password_hash("adminpass123")),
            )
            staff_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'staff', 1)",
                ("Staff", "staff@example.test", generate_password_hash("staffpass123")),
            ).lastrowid
            student_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'student', 1)",
                ("Student", "student@example.test", generate_password_hash("password123")),
            ).lastrowid
            now = datetime.now(timezone.utc)
            due_at = (now + timedelta(hours=72)).isoformat(timespec="seconds")
            issue_id = db.execute(
                "INSERT INTO issues (ticket_code, title, category, description, reporter_id, "
                "assigned_staff_id, status, priority, due_at, created_at, updated_at) "
                "VALUES ('CC-DEADLINE-1', 'Deadline test', 'Other', 'Test', ?, ?, 'Pending', "
                "'Medium', ?, ?, ?)",
                (student_id, staff_id, due_at, now.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")),
            ).lastrowid
            db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "remarks) VALUES (?, ?, NULL, 'Pending', 'Reported.')",
                (issue_id, student_id),
            )
            db.commit()
        self.login("admin@example.test", "adminpass123")
        detail = self.client.get(f"/issues/{issue_id}")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        response = self.client.post(f"/issues/{issue_id}/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Pending",
            "priority": "High",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            issue = db.execute("SELECT due_at FROM issues WHERE id = ?", (issue_id,)).fetchone()
            remaining = datetime.fromisoformat(issue["due_at"]) - datetime.now(timezone.utc)
            self.assertLessEqual(abs(remaining.total_seconds() - 24 * 3600), 5)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE type = 'deadline' AND user_id = ?",
                (staff_id,),
            ).fetchone()[0], 1)
        detail = self.client.get(f"/issues/{issue_id}")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', detail.data).group(1).decode()
        response = self.client.post(f"/issues/{issue_id}/admin-update", data={
            "csrf_token": csrf, "assigned_to": str(staff_id), "status": "Pending",
            "priority": "High", "custom_due_enabled": "on",
            "due_at": "2030-01-01T12:30",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            self.assertEqual(get_db().execute(
                "SELECT due_at FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()["due_at"], "2030-01-01T07:00:00+00:00")

    def test_admin_export_and_settings_are_role_protected(self):
        self.register_user("A Student", "student@example.test")
        self.login("student@example.test")
        self.assertEqual(self.client.get("/admin/issues.csv").status_code, 403)
        self.assertEqual(self.client.get("/admin/settings").status_code, 403)

    def test_staff_deactivation_unassigns_open_work_and_blocks_login(self):
        from app import get_db
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'admin', 1)",
                ("Admin", "admin@example.test", generate_password_hash("adminpass123")),
            )
            staff_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'staff', 1)",
                ("Staff", "staff@example.test", generate_password_hash("staffpass123")),
            ).lastrowid
            student_id = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_email_verified) "
                "VALUES (?, ?, ?, 'student', 1)",
                ("Student", "student@example.test", generate_password_hash("password123")),
            ).lastrowid
            now = "2026-01-01T00:00:00+00:00"
            issue_id = db.execute(
                "INSERT INTO issues (ticket_code, title, category, description, reporter_id, "
                "assigned_staff_id, status, due_at, created_at, updated_at) "
                "VALUES ('CC-DEACTIVATE', 'Assigned issue', 'Other', 'Test', ?, ?, 'Pending', ?, ?, ?)",
                (student_id, staff_id, "2030-01-01T00:00:00+00:00", now, now),
            ).lastrowid
            db.execute(
                "INSERT INTO issue_history (issue_id, changed_by_id, old_status, new_status, "
                "previous_staff_id, new_staff_id, remarks) "
                "VALUES (?, ?, NULL, 'Pending', NULL, ?, 'Reported.')",
                (issue_id, student_id, staff_id),
            )
            db.commit()
        self.login("admin@example.test", "adminpass123")
        csrf = self.csrf()
        response = self.client.post(f"/admin/staff/{staff_id}/toggle-active", data={
            "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from app import get_db
            db = get_db()
            self.assertEqual(db.execute(
                "SELECT assigned_staff_id FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()["assigned_staff_id"], None)
            self.assertEqual(db.execute(
                "SELECT is_active FROM users WHERE id = ?", (staff_id,)
            ).fetchone()["is_active"], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = 'assigned'",
                (student_id,),
            ).fetchone()[0], 1)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        response = self.login("staff@example.test", "staffpass123")
        self.assertIn(b"This account is inactive.", response.data)

    def test_demo_seed_command_creates_deadline_examples(self):
        from app import get_db
        from sla import urgency_for
        result = self.app.test_cli_runner().invoke(args=["seed-demo"])
        self.assertEqual(result.exit_code, 0, result.output)
        with self.app.app_context():
            db = get_db()
            issues = db.execute(
                "SELECT * FROM issues WHERE ticket_code IN "
                "('DEMO-OVERDUE', 'DEMO-DUE-SOON', 'DEMO-ON-TRACK') ORDER BY ticket_code"
            ).fetchall()
            self.assertEqual(len(issues), 3)
            by_code = {issue["ticket_code"]: urgency_for(issue, db) for issue in issues}
            self.assertEqual(by_code["DEMO-OVERDUE"], "Overdue")
            self.assertEqual(by_code["DEMO-DUE-SOON"], "Due soon")
            self.assertEqual(by_code["DEMO-ON-TRACK"], "On track")

    @staticmethod
    def _image_file(data):
        from io import BytesIO
        return BytesIO(data)

    @staticmethod
    def _valid_png():
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "08060000001f15c4890000000b49444154789c636000020000"
            "050001a5f645400000000049454e44ae426082"
        )

    @classmethod
    def _proof_uploads(cls):
        return {
            "proof_photo": (cls._image_file(cls._valid_png()), "repair.png"),
            "proof_video": (cls._image_file(b"\x00\x00\x00\x10ftypisom0000"), "repair.mp4"),
        }


if __name__ == "__main__":
    unittest.main()
