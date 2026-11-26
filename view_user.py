import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    sys.exit(f"Database not found: {path}")

db = sqlite3.connect(path)
db.row_factory = sqlite3.Row

rows = db.execute(
    "SELECT id, full_name, email, department FROM users "
    "WHERE role = 'student' ORDER BY id"
).fetchall()

for r in rows:
    print(r["id"], r["full_name"], r["email"], r["department"] or "-", sep=" | ")
print(f"Total students: {len(rows)}")
db.close()