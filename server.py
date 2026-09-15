#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
withdrawal-wireframe backend
============================

A real server-side backend for the hand-drawn "withdrawal wireframe" site:
user accounts, salted password hashing and server-issued session tokens,
persisted in SQLite.

Written against the Python 3 standard library only -- no pip installs, no
native compilation, so it runs anywhere Python 3.8+ exists.

Run
---
    python3 server.py                          # http://0.0.0.0:8000
    PORT=9000 DB_PATH=/tmp/app.db python3 server.py

API
---
    POST /api/signup   {email, password, confirm, name?}  -> 201 {user}  + session cookie
    POST /api/login    {email, password}                  -> 200 {user}  + session cookie
    POST /api/logout                                      -> 200 {}      - clears cookie
    GET  /api/session                                     -> 200 {user|null}
    GET  /api/stats                                       -> 200 {users}
    GET  /api/health                                      -> 200 {ok, users}

    -- withdrawal flow (all require a live session) --
    GET  /api/methods                                     -> 200 {methods[]}
    POST /api/withdrawal/quote  {amount}                  -> 200 {amount,fee,net,funds,limit}
    POST /api/withdrawal        {amount,method,destination} -> 201 {reference,...}
    GET  /api/withdrawal/history                          -> 200 {funds,limit,withdrawals[]}

Security notes
--------------
* Passwords are never stored, logged or returned. Each password gets its own
  random 16-byte salt and is stored as PBKDF2-HMAC-SHA256, 200k iterations.
* Session tokens are 32 random bytes; only the SHA-256 *hash* of the token is
  written to the database, so a database leak yields no usable sessions.
* Session verification uses hmac.compare_digest (constant time).
* The session cookie is HttpOnly + SameSite=Lax (and Secure when the request
  arrives over HTTPS), so page scripts cannot read it and client-side state
  is never trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookies
import http.server
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "data", "app.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

COOKIE_NAME = "wf_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60          # 30 days
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
MIN_PASSWORD = 5
MAX_BODY_BYTES = 64 * 1024

# ---- withdrawal product configuration (server-side source of truth) -------
OPENING_FUNDS_CENTS = 10_000_000        # $100,000.00 available funds
OPENING_LIMIT_CENTS = 5_500_000         # $55,000.00 available limit
MIN_WITHDRAWAL_CENTS = 1_000            # $10.00
FEE_BPS = 100                           # 1.00% processing fee
FEE_MIN_CENTS = 100                     # $1.00 minimum fee

METHODS = [
    {"id": "cashapp", "label": "CashApp",    "handle": "$cashtag",        "etaMinutes": 5},
    {"id": "paypal",  "label": "PayPal",     "handle": "email address",   "etaMinutes": 15},
    {"id": "chime",   "label": "Chime",      "handle": "$chimesign",      "etaMinutes": 10},
    {"id": "zelle",   "label": "Zelle",      "handle": "email or phone",  "etaMinutes": 10},
    {"id": "btc",     "label": "BTC",        "handle": "wallet address",  "etaMinutes": 30},
    {"id": "bank",    "label": "Local bank", "handle": "account number",  "etaMinutes": 120},
]
METHOD_IDS = {m["id"] for m in METHODS}

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$")

# One source of truth for the user-facing wording. The front end renders
# whatever message the server returns, so the copy can never drift.
MSG = {
    "signup.email.empty": "Enter your email address.",
    "signup.email.invalid": "That email does not look right \u2014 use the format name@example.com.",
    "signup.pass.short": "Password needs at least %d characters." % MIN_PASSWORD,
    "signup.confirm.mismatch": "The two passwords do not match.",
    "signup.duplicate": "That email is already registered \u2014 switch to Log in.",
    "login.email.empty": "Enter the email you signed up with.",
    "login.email.invalid": "That email does not look right \u2014 use the format name@example.com.",
    "login.pass.empty": "Enter your password.",
    "login.notfound": "No account found for that email \u2014 sign up first.",
    "login.badpass": "Incorrect password \u2014 try again.",
    "login.throttled": "Too many attempts \u2014 wait a few minutes and try again.",
    "auth.required": "Your session has expired \u2014 please sign in again.",
    "wd.amount.empty": "Enter the amount you want to withdraw.",
    "wd.amount.invalid": "Enter a valid amount, for example 250.00.",
    "wd.amount.min": "The smallest withdrawal is $10.00.",
    "wd.amount.overlimit": "That is more than your available limit.",
    "wd.amount.overfunds": "That is more than your available funds.",
    "wd.method.invalid": "Choose one of the supported withdrawal methods.",
    "wd.destination.empty": "Enter where the funds should be sent.",
    "wd.destination.invalid": "That destination does not look right for the method you picked.",
    "server.error": "Something went wrong on the server. Please try again.",
}

# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

_db_lock = threading.Lock()


def connect():
    """A fresh connection per request: SQLite objects are not thread-safe."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _db_lock, connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT    NOT NULL UNIQUE,
                name        TEXT    NOT NULL DEFAULT '',
                pw_hash     TEXT    NOT NULL,
                pw_salt     TEXT    NOT NULL,
                iterations  INTEGER NOT NULL,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash  TEXT    PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  TEXT    NOT NULL,
                expires_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS withdrawals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reference    TEXT    NOT NULL UNIQUE,
                amount_cents INTEGER NOT NULL,
                fee_cents    INTEGER NOT NULL,
                net_cents    INTEGER NOT NULL,
                method       TEXT    NOT NULL,
                destination  TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, created_at DESC);
            """
        )
        conn.commit()


# --------------------------------------------------------------------------
# password + token primitives
# --------------------------------------------------------------------------

def hash_password(password: str, salt_hex: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    )
    return dk.hex()


def verify_password(password: str, user_row) -> bool:
    try:
        expected = user_row["pw_hash"]
        candidate = hash_password(password, user_row["pw_salt"], int(user_row["iterations"]))
    except Exception:
        return False
    return hmac.compare_digest(candidate, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def name_from_email(email: str) -> str:
    local = (email.split("@")[0] or "").strip()
    words = [w for w in re.split(r"[._+\-]+", local) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


# --------------------------------------------------------------------------
# money helpers
# --------------------------------------------------------------------------

def cents_to_str(cents: int) -> str:
    return "$%s" % format(cents / 100.0, ",.2f")


def parse_amount_to_cents(raw):
    """'250', '250.5', '$1,250.00' -> cents. None when not a valid amount."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if not text or not re.fullmatch(r"\d{1,12}(\.\d{1,2})?", text):
        return None
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def compute_fee_cents(amount_cents: int) -> int:
    return max(FEE_MIN_CENTS, (amount_cents * FEE_BPS) // 10_000)


def account_balances(conn, user_id: int) -> dict:
    """Opening funds / limit minus everything already requested."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS used
           FROM withdrawals
           WHERE user_id = ? AND status IN ('pending', 'completed')""",
        (user_id,),
    ).fetchone()
    used = int(row["used"] or 0)
    return {
        "fundsCents": max(0, OPENING_FUNDS_CENTS - used),
        "limitCents": max(0, OPENING_LIMIT_CENTS - used),
        "usedCents": used,
    }


def validate_destination(method_id: str, destination: str):
    """(ok, normalised) -- lightweight per-method sanity check."""
    dest = (destination or "").strip()
    if not dest:
        return False, ""
    if method_id in ("cashapp", "chime"):
        return bool(re.fullmatch(r"\$[A-Za-z][A-Za-z0-9_]{2,24}", dest)), dest
    if method_id in ("paypal", "zelle"):
        if re.fullmatch(r"\+?\d{7,15}", dest.replace(" ", "").replace("-", "")):
            return True, dest
        return bool(EMAIL_RE.match(dest.lower())), dest
    if method_id == "btc":
        return bool(re.fullmatch(r"(bc1[A-Za-z0-9]{25,62}|[13][A-Za-z0-9]{25,34})", dest)), dest
    if method_id == "bank":
        digits = re.sub(r"\D", "", dest)
        return 6 <= len(digits) <= 18, dest
    return False, dest


# --------------------------------------------------------------------------
# login throttle (per client IP, in-memory)
# --------------------------------------------------------------------------

_THROTTLE = {}
_THROTTLE_LOCK = threading.Lock()
MAX_ATTEMPTS = 15
ATTEMPT_WINDOW = 300          # seconds


def throttle_blocked(ip: str) -> bool:
    now = time.time()
    with _THROTTLE_LOCK:
        hits = [t for t in _THROTTLE.get(ip, []) if now - t < ATTEMPT_WINDOW]
        _THROTTLE[ip] = hits
        return len(hits) >= MAX_ATTEMPTS


def throttle_record(ip: str) -> None:
    now = time.time()
    with _THROTTLE_LOCK:
        hits = [t for t in _THROTTLE.get(ip, []) if now - t < ATTEMPT_WINDOW]
        hits.append(now)
        _THROTTLE[ip] = hits


def throttle_clear(ip: str) -> None:
    with _THROTTLE_LOCK:
        _THROTTLE.pop(ip, None)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wireframe-backend/2.0"

    # ---- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):          # quieter, single-line logs
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip()
        return proto.lower() == "https"

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json")
                         else "no-cache")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _error(self, status: int, code: str, message: str, extra: dict | None = None) -> None:
        self._json(status, {"error": {"code": code, "message": message}}, extra)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie")
        if not raw:
            return {}
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return {}
        return {k: v.value for k, v in jar.items()}

    # ---- sessions -------------------------------------------------------

    def _session_cookie(self, token: str) -> str:
        parts = [
            "%s=%s" % (COOKIE_NAME, token),
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=%d" % SESSION_TTL_SECONDS,
        ]
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_cookie(self) -> str:
        parts = ["%s=" % COOKIE_NAME, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _issue_session(self, conn, user_id: int) -> str:
        token = new_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token_hash(token), user_id, now_iso(), int(time.time()) + SESSION_TTL_SECONDS),
        )
        conn.commit()
        return token

    def _current_user(self):
        """Resolve the cookie token to a live user row, or None."""
        token = self._cookies().get(COOKIE_NAME)
        if not token:
            return None
        with connect() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.email, u.name, u.created_at, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash(token),),
            ).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) <= int(time.time()):
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
                conn.commit()
                return None
            return row

    @staticmethod
    def _public_user(row) -> dict:
        return {
            "email": row["email"],
            "name": row["name"] or name_from_email(row["email"]),
            "createdAt": row["created_at"],
        }

    # ---- routes ---------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/health":
            try:
                with connect() as conn:
                    n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                return self._json(200, {"ok": True, "users": n, "storage": "sqlite"})
            except Exception:
                return self._error(500, "server.error", MSG["server.error"])

        if path == "/api/stats":
            with connect() as conn:
                n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                s = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE expires_at > ?",
                    (int(time.time()),),
                ).fetchone()["n"]
            return self._json(200, {"users": n, "activeSessions": s})

        if path == "/api/session":
            row = self._current_user()
            if row is None:
                return self._json(200, {"user": None})
            return self._json(200, {"user": self._public_user(row)})

        if path == "/api/methods":
            if self._current_user() is None:
                return self._error(401, "auth.required", MSG["auth.required"])
            return self._json(200, {"methods": METHODS})

        if path == "/api/withdrawal/history":
            return self._handle_history()

        if path.startswith("/api/"):
            return self._error(404, "not_found", "Unknown endpoint.")

        return self._serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/signup":
            return self._handle_signup()
        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()
        if path == "/api/withdrawal/quote":
            return self._handle_quote()
        if path == "/api/withdrawal":
            return self._handle_withdrawal()
        return self._error(404, "not_found", "Unknown endpoint.")

    # ---- auth handlers --------------------------------------------------

    def _handle_signup(self):
        data = self._read_json()
        if data is None:
            return self._error(400, "bad_request", MSG["server.error"])

        email = str(data.get("email") or "").strip().lower()
        password = str(data.get("password") or "")
        confirm = data.get("confirm")
        name = str(data.get("name") or "").strip()

        if not email:
            return self._error(400, "signup.email.empty", MSG["signup.email.empty"])
        if not EMAIL_RE.match(email):
            return self._error(400, "signup.email.invalid", MSG["signup.email.invalid"])
        if len(password) < MIN_PASSWORD:
            return self._error(400, "signup.pass.short", MSG["signup.pass.short"])
        if confirm is not None and password != str(confirm):
            return self._error(400, "signup.confirm.mismatch", MSG["signup.confirm.mismatch"])
        if not name:
            name = name_from_email(email)

        salt = secrets.token_hex(SALT_BYTES)
        pw_hash = hash_password(password, salt)

        try:
            with connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM users WHERE email = ?", (email,)
                ).fetchone()
                if exists:
                    return self._error(409, "signup.duplicate", MSG["signup.duplicate"])
                cur = conn.execute(
                    """INSERT INTO users (email, name, pw_hash, pw_salt, iterations, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (email, name, pw_hash, salt, PBKDF2_ITERATIONS, now_iso()),
                )
                conn.commit()
                user_id = cur.lastrowid
                token = self._issue_session(conn, user_id)
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        except sqlite3.IntegrityError:
            # lost a race against a concurrent signup for the same email
            return self._error(409, "signup.duplicate", MSG["signup.duplicate"])
        except Exception as exc:                       # pragma: no cover
            print("signup failed: %r" % (exc,), flush=True)
            return self._error(500, "server.error", MSG["server.error"])

        throttle_clear(self._client_ip())
        return self._json(
            201,
            {"user": self._public_user(row), "message": "Account created."},
            {"Set-Cookie": self._session_cookie(token)},
        )

    def _handle_login(self):
        ip = self._client_ip()
        if throttle_blocked(ip):
            return self._error(429, "login.throttled", MSG["login.throttled"])

        data = self._read_json()
        if data is None:
            return self._error(400, "bad_request", MSG["server.error"])

        email = str(data.get("email") or "").strip().lower()
        password = str(data.get("password") or "")

        if not email:
            return self._error(400, "login.email.empty", MSG["login.email.empty"])
        if not EMAIL_RE.match(email):
            return self._error(400, "login.email.invalid", MSG["login.email.invalid"])
        if not password:
            return self._error(400, "login.pass.empty", MSG["login.pass.empty"])

        with connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is None:
                throttle_record(ip)
                return self._error(404, "login.notfound", MSG["login.notfound"])
            if not verify_password(password, row):
                throttle_record(ip)
                return self._error(401, "login.badpass", MSG["login.badpass"])

            token = self._issue_session(conn, row["id"])

        throttle_clear(ip)
        return self._json(
            200,
            {"user": self._public_user(row), "message": "Signed in."},
            {"Set-Cookie": self._session_cookie(token)},
        )

    def _handle_logout(self):
        token = self._cookies().get(COOKIE_NAME)
        if token:
            try:
                with connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
                    conn.commit()
            except Exception:
                pass
        return self._json(
            200,
            {"ok": True, "user": None},
            {"Set-Cookie": self._clear_cookie()},
        )

    # ---- withdrawal handlers --------------------------------------------

    def _require_user(self):
        """Return the live user row, or None after writing a 401."""
        row = self._current_user()
        if row is None:
            self._error(401, "auth.required", MSG["auth.required"])
            return None
        return row

    def _handle_quote(self):
        row = self._require_user()
        if row is None:
            return
        data = self._read_json() or {}
        amount = parse_amount_to_cents(data.get("amount"))
        if amount is None:
            return self._error(400, "wd.amount.invalid", MSG["wd.amount.invalid"])
        with connect() as conn:
            bal = account_balances(conn, row["id"])
        fee = compute_fee_cents(amount)
        return self._json(200, {
            "amountCents": amount,
            "feeCents": fee,
            "netCents": amount - fee,
            "amount": cents_to_str(amount),
            "fee": cents_to_str(fee),
            "net": cents_to_str(amount - fee),
            "funds": cents_to_str(bal["fundsCents"]),
            "limit": cents_to_str(bal["limitCents"]),
            "minCents": MIN_WITHDRAWAL_CENTS,
            "min": cents_to_str(MIN_WITHDRAWAL_CENTS),
            "feeNote": "%.2f%% processing fee, %s minimum" % (FEE_BPS / 100.0, cents_to_str(FEE_MIN_CENTS)),
        })

    def _handle_history(self):
        row = self._require_user()
        if row is None:
            return
        with connect() as conn:
            bal = account_balances(conn, row["id"])
            rows = conn.execute(
                """SELECT reference, amount_cents, fee_cents, net_cents, method,
                          destination, status, created_at
                   FROM withdrawals WHERE user_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 50""",
                (row["id"],),
            ).fetchall()
        items = []
        for r in rows:
            items.append({
                "reference": r["reference"],
                "amount": cents_to_str(int(r["amount_cents"])),
                "fee": cents_to_str(int(r["fee_cents"])),
                "net": cents_to_str(int(r["net_cents"])),
                "method": r["method"],
                "methodLabel": next((m["label"] for m in METHODS if m["id"] == r["method"]), r["method"]),
                "destination": r["destination"],
                "status": r["status"],
                "createdAt": r["created_at"],
            })
        return self._json(200, {
            "funds": cents_to_str(bal["fundsCents"]),
            "limit": cents_to_str(bal["limitCents"]),
            "fundsCents": bal["fundsCents"],
            "limitCents": bal["limitCents"],
            "withdrawals": items,
        })

    def _handle_withdrawal(self):
        row = self._require_user()
        if row is None:
            return
        data = self._read_json()
        if data is None:
            return self._error(400, "bad_request", MSG["server.error"])

        raw_amount = str(data.get("amount") or "").strip()
        amount = parse_amount_to_cents(raw_amount)
        method = str(data.get("method") or "").strip().lower()
        destination = str(data.get("destination") or "").strip()

        if amount is None:
            if not raw_amount:
                return self._error(400, "wd.amount.empty", MSG["wd.amount.empty"])
            return self._error(400, "wd.amount.invalid", MSG["wd.amount.invalid"])
        if amount < MIN_WITHDRAWAL_CENTS:
            return self._error(400, "wd.amount.min", MSG["wd.amount.min"])
        if method not in METHOD_IDS:
            return self._error(400, "wd.method.invalid", MSG["wd.method.invalid"])
        if not destination:
            return self._error(400, "wd.destination.empty", MSG["wd.destination.empty"])
        ok, dest = validate_destination(method, destination)
        if not ok:
            return self._error(400, "wd.destination.invalid", MSG["wd.destination.invalid"])

        fee = compute_fee_cents(amount)
        net = amount - fee
        created = now_iso()

        try:
            with connect() as conn:
                bal = account_balances(conn, row["id"])
                if amount > bal["limitCents"]:
                    return self._error(400, "wd.amount.overlimit", MSG["wd.amount.overlimit"])
                if amount > bal["fundsCents"]:
                    return self._error(400, "wd.amount.overfunds", MSG["wd.amount.overfunds"])
                reference = "WD-" + secrets.token_hex(4).upper()
                conn.execute(
                    """INSERT INTO withdrawals
                       (user_id, reference, amount_cents, fee_cents, net_cents,
                        method, destination, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row["id"], reference, amount, fee, net, method, dest, "pending", created),
                )
                conn.commit()
                after = account_balances(conn, row["id"])
        except Exception as exc:
            print("withdrawal failed: %r" % (exc,), flush=True)
            return self._error(500, "server.error", MSG["server.error"])

        label = next((m["label"] for m in METHODS if m["id"] == method), method)
        return self._json(201, {
            "reference": reference,
            "status": "pending",
            "amount": cents_to_str(amount),
            "fee": cents_to_str(fee),
            "net": cents_to_str(net),
            "amountCents": amount,
            "feeCents": fee,
            "netCents": net,
            "method": method,
            "methodLabel": label,
            "destination": dest,
            "funds": cents_to_str(after["fundsCents"]),
            "limit": cents_to_str(after["limitCents"]),
            "fundsCents": after["fundsCents"],
            "limitCents": after["limitCents"],
            "createdAt": created,
        })

    # ---- static files ---------------------------------------------------

    def _serve_static(self, path: str):
        rel = urllib_unquote(path).lstrip("/")
        if rel in ("", "/"):
            rel = "index.html"

        candidate = os.path.realpath(os.path.join(BASE_DIR, rel))
        if not candidate.startswith(os.path.realpath(BASE_DIR) + os.sep):
            return self._error(403, "forbidden", "Forbidden.")

        if not os.path.isfile(candidate):
            if "." not in os.path.basename(rel):
                return self._serve_static("/index.html")
            return self._error(404, "not_found", "Not found.")

        ctype = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json", "image/svg+xml"
        ):
            ctype += "; charset=utf-8"

        with open(candidate, "rb") as handle:
            body = handle.read()

        extra = {}
        if ctype.startswith("text/html"):
            extra["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
        return self._send(200, body, ctype, extra)


def urllib_unquote(value: str) -> str:
    from urllib.parse import unquote
    return unquote(value)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    init_db()
    httpd = Server((HOST, PORT), Handler)
    print("withdrawal-wireframe backend", flush=True)
    print("  database : %s" % DB_PATH, flush=True)
    print("  listening: http://%s:%d" % (HOST, PORT), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
