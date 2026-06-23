#!/usr/bin/env python3
"""
Prestige Fabrications - Enquiry, Quoting & Estimating Control
Phase 2 backend.

A self-contained web server using ONLY the Python standard library, so it runs
on any machine with Python 3.8+ installed - no pip install, no Node, no external
services. It provides:

  * Real per-user logins (hashed passwords + session cookies) -> accurate audit trail
  * Persistent storage in a single SQLite file (prestige.db) - data survives restarts
  * File storage on disk (./uploads) for drawings, CAD files, quotes, POs
  * A JSON REST API the browser app talks to
  * Serves the single-page app (index.html) itself

Run:
    python3 app.py
Then open:
    http://localhost:8000

Default logins (username / password) - change these before any real use:
    sarah / sarah   (Admin)
    mark  / mark    (Estimator)
    priya / priya   (Estimator)
    dave  / dave    (Sales/Admin)
    janet / janet   (Management)
"""

import os
import re
import json
import sqlite3
import hashlib
import secrets
import base64
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR holds the database + uploaded files. On hosts with an ephemeral
# filesystem (e.g. Railway), set DATA_DIR to a mounted persistent volume
# (e.g. DATA_DIR=/data) so data survives restarts and re-deploys.
DATA_DIR = os.environ.get("DATA_DIR", HERE)
DB_PATH = os.path.join(DATA_DIR, "prestige.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
INDEX_PATH = os.path.join(HERE, "index.html")
PORT = int(os.environ.get("PORT", "8000"))
# Demo/sample data is OFF by default for a real deployment. Set LOAD_DEMO_DATA=1
# to pre-fill the board with example enquiries (useful for a trial or training).
LOAD_DEMO_DATA = os.environ.get("LOAD_DEMO_DATA", "0").strip().lower() in ("1", "true", "yes", "on")

# Users mirror the front-end roster so ids/names/colours line up everywhere.
# ---------------------------------------------------------------------------
# ACCOUNTS — edit this list to set up your real team, then redeploy.
# Each row:  ("uID", "Full Name", "Role", "#hexcolour", "username", "default-password")
#   id        : a unique short id, u1, u2, u3 ...  (don't reuse one)
#   role      : one of  Admin | Estimator | Sales/Admin | Management
#               (Admin can override duplicate SigmaMRP numbers; Management can
#                approve high-value quotes; all roles can see/progress everything)
#   colour    : the avatar colour, any hex value
#   username  : what they type to log in (lowercase, no spaces)
#   password  : a starting password ONLY used if no PF_PW_<USERNAME> variable is
#               set. On a live site, set PF_PW_<USERNAME> in Railway instead.
# Accounts are created/updated on each deploy. Removing a row does not delete
# that account (to protect its records) — wipe the volume to fully reset.
# ---------------------------------------------------------------------------
SEED_USERS = [
    # id,   name,          role,          colour,    username, password
    ("u1", "Sam Mckelvie",  "Admin",       "#7A5BD0", "sam", "1234"),
    ("u2", "Mark Reilly", "Estimator",   "#3B72C9", "mark",  "mark"),
    ("u3", "Priya Patel", "Estimator",   "#2E8F66", "priya", "priya"),
    ("u4", "Dave Holt",   "Sales/Admin", "#E2622C", "dave",  "dave"),
    ("u5", "Janet Cole",  "Management",  "#CF4B45", "janet", "janet"),
]

SEED_SUPPLIERS = [
    ("s1", "Wedge Galvanising"),
    ("s2", "Penrith Powder Coating"),
    ("s3", "Northern Laser Profiles"),
    ("s4", "Atlas Steel Stockholders"),
    ("s5", "Sigma CNC Subcontract"),
]

_db_lock = threading.Lock()
_sessions = {}  # token -> user_id  (in-memory; users simply log in again after a restart)


# --------------------------------------------------------------------------- #
#  Database
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return h.hex(), salt


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, name TEXT, role TEXT, colour TEXT,
                username TEXT UNIQUE, pw_hash TEXT, pw_salt TEXT
            );
            CREATE TABLE IF NOT EXISTS enquiries(
                id TEXT PRIMARY KEY, sigma TEXT, customer TEXT, status TEXT,
                data TEXT, updated_at TEXT, updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, type TEXT, txt TEXT, enq TEXT,
                "when" TEXT, read INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS suppliers(
                id TEXT PRIMARY KEY, name TEXT
            );
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # Sync the user accounts from SEED_USERS on every boot:
        #  - create any account that doesn't exist yet
        #  - keep each existing account's name / role / colour up to date
        #  - set the password from PF_PW_<USERNAME> if that variable is provided
        # This means you can add or edit people in SEED_USERS, redeploy, and the
        # accounts appear/update without wiping the database. (Removing a person
        # from the list does NOT delete their account, to avoid orphaning records.)
        for uid, name, role, colour, username, default_pw in SEED_USERS:
            override = os.environ.get("PF_PW_" + username.upper())
            existing = conn.execute(
                "SELECT id FROM users WHERE username=?", (username,)
            ).fetchone()
            if existing is None:
                ph, salt = hash_pw(override or default_pw)
                conn.execute(
                    "INSERT INTO users(id,name,role,colour,username,pw_hash,pw_salt)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (uid, name, role, colour, username, ph, salt),
                )
            else:
                conn.execute(
                    "UPDATE users SET name=?, role=?, colour=? WHERE username=?",
                    (name, role, colour, username),
                )
                if override:
                    ph, salt = hash_pw(override)
                    conn.execute(
                        "UPDATE users SET pw_hash=?, pw_salt=? WHERE username=?",
                        (ph, salt, username),
                    )
        # seed suppliers
        if not conn.execute("SELECT 1 FROM suppliers LIMIT 1").fetchone():
            conn.executemany("INSERT INTO suppliers(id,name) VALUES(?,?)", SEED_SUPPLIERS)
        # kv defaults
        if not conn.execute("SELECT value FROM kv WHERE key='enq_seq'").fetchone():
            conn.execute("INSERT INTO kv(key,value) VALUES('enq_seq','131')")
        if not conn.execute("SELECT value FROM kv WHERE key='seeded'").fetchone():
            conn.execute("INSERT INTO kv(key,value) VALUES('seeded','0')")
        conn.commit()


def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn, key, value):
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
#  Helpers shared by handlers
# --------------------------------------------------------------------------- #
def user_public(row):
    return {"id": row["id"], "name": row["name"], "role": row["role"], "c": row["colour"]}


def enquiry_full(row):
    d = json.loads(row["data"])
    d["id"] = row["id"]
    return d


def save_enquiry(conn, doc, updated_by):
    """Upsert one enquiry document. The rich nested doc lives in the data column;
    a few hot fields are mirrored into columns for indexing / duplicate checks."""
    eid = doc["id"]
    conn.execute(
        "INSERT INTO enquiries(id,sigma,customer,status,data,updated_at,updated_by) "
        "VALUES(?,?,?,?,?,datetime('now'),?) "
        "ON CONFLICT(id) DO UPDATE SET sigma=excluded.sigma,customer=excluded.customer,"
        "status=excluded.status,data=excluded.data,updated_at=excluded.updated_at,"
        "updated_by=excluded.updated_by",
        (eid, str(doc.get("sigma", "")), doc.get("customer", ""),
         doc.get("status", ""), json.dumps(doc), updated_by),
    )


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "PrestigeControl/2.0"

    # -- low-level response helpers ---------------------------------------- #
    def _json(self, obj, status=200, set_cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status, msg):
        self._json({"error": msg}, status=status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode())
        except Exception:
            return {}

    def _current_user(self, conn):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie["pf_session"].value if "pf_session" in cookie else None
        uid = _sessions.get(token)
        if not uid:
            return None
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- routing ----------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api_get(path)
        if path.startswith("/uploads/"):
            return self.serve_upload(path)
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api_post(path)
        return self._err(404, "not found")

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api_put(path)
        return self._err(404, "not found")

    # -- static / uploads -------------------------------------------------- #
    def serve_static(self, path):
        if path in ("/", "/index.html"):
            target = INDEX_PATH
        else:
            target = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
            if not target.startswith(HERE):
                return self._err(403, "forbidden")
        if not os.path.isfile(target):
            target = INDEX_PATH  # SPA fallback
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_upload(self, path):
        name = os.path.basename(path)
        target = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(target):
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- API GET ----------------------------------------------------------- #
    def api_get(self, path):
        with _db_lock, db() as conn:
            if path == "/api/me":
                u = self._current_user(conn)
                if not u:
                    return self._err(401, "not logged in")
                return self._json({"user": user_public(u)})

            if path == "/api/state":
                u = self._current_user(conn)
                if not u:
                    return self._err(401, "not logged in")
                users = [user_public(r) for r in conn.execute("SELECT * FROM users").fetchall()]
                enquiries = [enquiry_full(r) for r in
                             conn.execute("SELECT * FROM enquiries ORDER BY id DESC").fetchall()]
                notifs = [dict(r) for r in
                          conn.execute('SELECT * FROM notifications ORDER BY id DESC').fetchall()]
                # normalise notification rows to the shape the UI expects
                notifs = [{"id": n["id"], "user": n["user_id"], "type": n["type"],
                           "txt": n["txt"], "enq": n["enq"], "when": n["when"],
                           "read": bool(n["read"])} for n in notifs]
                suppliers = [dict(r) for r in conn.execute("SELECT * FROM suppliers").fetchall()]
                seeded = kv_get(conn, "seeded", "0") == "1"
                return self._json({
                    "user": user_public(u),
                    "users": users,
                    "enquiries": enquiries,
                    "notifications": notifs,
                    "suppliers": suppliers,
                    "needsSeed": LOAD_DEMO_DATA and (not seeded) and len(enquiries) == 0,
                })

            if path == "/api/sigma-check":
                qs = parse_qs(urlparse(self.path).query)
                n = (qs.get("n") or [""])[0].strip()
                exclude = (qs.get("exclude") or [""])[0]
                rows = conn.execute(
                    "SELECT id,status FROM enquiries WHERE sigma=? AND id!=?", (n, exclude)
                ).fetchall()
                active = [r["id"] for r in rows
                          if r["status"] not in ("Lost", "No Quote", "Dormant")]
                return self._json({"duplicate": len(active) > 0, "on": active})

        return self._err(404, "not found")

    # -- API POST ---------------------------------------------------------- #
    def api_post(self, path):
        body = self._read_json()

        # login is the one endpoint that does not need an existing session
        if path == "/api/login":
            with _db_lock, db() as conn:
                username = (body.get("username") or "").strip().lower()
                password = body.get("password") or ""
                row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                if not row:
                    return self._err(401, "Unknown username")
                calc, _ = hash_pw(password, row["pw_salt"])
                if calc != row["pw_hash"]:
                    return self._err(401, "Wrong password")
                token = secrets.token_hex(24)
                _sessions[token] = row["id"]
                cookie = f"pf_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
                return self._json({"user": user_public(row)}, set_cookie=cookie)

        with _db_lock, db() as conn:
            u = self._current_user(conn)
            if not u:
                return self._err(401, "not logged in")
            me = user_public(u)

            if path == "/api/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                if "pf_session" in cookie:
                    _sessions.pop(cookie["pf_session"].value, None)
                return self._json({"ok": True})

            if path == "/api/seed":
                # one-time import of the demo dataset shipped with the front-end
                if kv_get(conn, "seeded", "0") == "1":
                    return self._json({"ok": True, "skipped": True})
                for doc in body.get("enquiries", []):
                    save_enquiry(conn, doc, me["id"])
                for n in body.get("notifications", []):
                    conn.execute(
                        'INSERT INTO notifications(user_id,type,txt,enq,"when",read) '
                        "VALUES(?,?,?,?,?,?)",
                        (n.get("user"), n.get("type"), n.get("txt"),
                         n.get("enq"), n.get("when"), 1 if n.get("read") else 0),
                    )
                kv_set(conn, "seeded", "1")
                conn.commit()
                return self._json({"ok": True})

            if path == "/api/enquiries":
                # create: server owns the id sequence + duplicate guard
                doc = body.get("data", {})
                sigma = str(doc.get("sigma", "")).strip()
                override = body.get("override", False)
                if not override and sigma:
                    rows = conn.execute(
                        "SELECT id,status FROM enquiries WHERE sigma=?", (sigma,)
                    ).fetchall()
                    active = [r["id"] for r in rows
                              if r["status"] not in ("Lost", "No Quote", "Dormant")]
                    if active:
                        return self._json({"duplicate": True, "on": active}, status=409)
                seq = int(kv_get(conn, "enq_seq", "131")) + 1
                kv_set(conn, "enq_seq", seq)
                eid = "ENQ-2026-" + str(seq).zfill(6)
                doc["id"] = eid
                save_enquiry(conn, doc, me["id"])
                conn.commit()
                return self._json({"enquiry": doc})

            if path == "/api/notifications":
                conn.execute(
                    'INSERT INTO notifications(user_id,type,txt,enq,"when",read) '
                    "VALUES(?,?,?,?,datetime('now'),0)",
                    (body.get("user"), body.get("type"), body.get("txt"), body.get("enq")),
                )
                conn.commit()
                return self._json({"ok": True})

            if path == "/api/notifications/read":
                conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (me["id"],))
                conn.commit()
                return self._json({"ok": True})

            if path == "/api/suppliers":
                sid = "s" + secrets.token_hex(4)
                conn.execute("INSERT INTO suppliers(id,name) VALUES(?,?)",
                             (sid, body.get("name", "").strip()))
                conn.commit()
                return self._json({"id": sid, "name": body.get("name", "").strip()})

            if path == "/api/uploads":
                # files arrive base64-encoded inside JSON (keeps us stdlib-only & robust)
                raw = body.get("b64", "")
                if "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    data = base64.b64decode(raw)
                except Exception:
                    return self._err(400, "bad file data")
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", body.get("name", "file"))
                fname = secrets.token_hex(6) + "_" + safe
                with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                    f.write(data)
                return self._json({"name": body.get("name", safe),
                                   "url": "/uploads/" + fname,
                                   "size": len(data)})

        return self._err(404, "not found")

    # -- API PUT ----------------------------------------------------------- #
    def api_put(self, path):
        m = re.match(r"^/api/enquiries/(ENQ-[\w-]+)$", path)
        if not m:
            return self._err(404, "not found")
        body = self._read_json()
        with _db_lock, db() as conn:
            u = self._current_user(conn)
            if not u:
                return self._err(401, "not logged in")
            doc = body.get("data", {})
            doc["id"] = m.group(1)
            save_enquiry(conn, doc, u["id"])
            conn.commit()
            return self._json({"ok": True})


# --------------------------------------------------------------------------- #
#  Entrypoint
# --------------------------------------------------------------------------- #
def main():
    init_db()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 62)
    print("  Prestige Fabrications - Enquiry Control")
    print("=" * 62)
    print(f"  Running at:   http://localhost:{PORT}")
    print(f"  Data dir:     {DATA_DIR}")
    print(f"  Database:     {DB_PATH}")
    print(f"  File storage: {UPLOAD_DIR}")
    print(f"  Demo data:    {'ON (LOAD_DEMO_DATA set)' if LOAD_DEMO_DATA else 'off'}")
    # warn about any account still using its default (username) password
    insecure = [u for (_, _, _, _, u, _) in SEED_USERS if not os.environ.get("PF_PW_" + u.upper())]
    if insecure:
        print("  " + "-" * 58)
        print("  SECURITY: these accounts still use the default password")
        print("  (same as the username). Set a variable to secure each one,")
        print("  e.g. PF_PW_" + insecure[0].upper() + "=your-password, then redeploy:")
        print("    " + ", ".join(insecure))
    else:
        print("  Logins:       all account passwords set via PF_PW_* variables")
    print("  Press Ctrl+C to stop.")
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
