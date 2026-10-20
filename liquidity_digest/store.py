"""Private append-only captures and durable SMTP delivery receipts."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .model import NY, UTC, stamp


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def atomic_json(path, value):
    path = Path(path)
    serialized = encode(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        temporary = stream.name
        stream.write(serialized)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "captures.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY, key TEXT NOT NULL, captured_at TEXT NOT NULL,
                    status TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS capture_key_time ON captures(key,captured_at);
                CREATE TABLE IF NOT EXISTS deliveries (
                    report_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, message_id TEXT NOT NULL, detail TEXT);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self, name):
        path = self.root / (name + ".lock")
        with path.open("a") as handle:
            os.chmod(path, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def append(self, key, status, data, source, now=None):
        captured = stamp(now or datetime.now(UTC)).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO captures(key,captured_at,status,source,payload) VALUES(?,?,?,?,?)",
                       (key, captured, status, source, encode(data)))

    def snapshot(self, cutoff):
        cutoff = stamp(cutoff)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM captures WHERE captured_at<=? ORDER BY captured_at,id",
                              (cutoff.isoformat(),)).fetchall()
        successful, latest = {}, {}
        for row in rows:
            latest[row["key"]] = row
            if row["status"] == "OK":
                successful[row["key"]] = row
        output = {}
        for key, last in latest.items():
            good = successful.get(key)
            status = last["status"]
            if good:
                if stamp(good["captured_at"]).astimezone(NY).date() != cutoff.astimezone(NY).date():
                    status = "CACHED"
                elif last["status"] != "OK":
                    status = "CACHED_AFTER_ERROR"
                output[key] = {"data": json.loads(good["payload"]), "status": status,
                               "source": good["source"], "captured_at": good["captured_at"],
                               "attempt_at": last["captured_at"]}
            else:
                output[key] = {"data": [], "status": status, "source": last["source"],
                               "captured_at": None, "attempt_at": last["captured_at"]}
        return output

    def reserve_delivery(self, day, message_id):
        with self.connect() as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?)",
                       (day, "PENDING", datetime.now(UTC).isoformat(), message_id, ""))

    def delivery(self, day):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE report_date=?", (day,)).fetchone()
        return dict(row) if row else None

    def mark_delivery(self, day, status, detail=""):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=?,updated_at=?,detail=? WHERE report_date=?",
                       (status, datetime.now(UTC).isoformat(), detail, day))
