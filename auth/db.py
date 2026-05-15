import hashlib
import os
import secrets
import smtplib
import sqlite3
import uuid
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_DB_PATH = Path(os.getenv("AUTH_DB", str(_ROOT / "auth.db")))

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "") or SMTP_USER
APP_URL       = os.getenv("APP_URL", "http://localhost:8001")

SESSION_DAYS = 7
RESET_HOURS  = 1


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                surname       TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reset_tokens (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
            );
        """)


# ── Password hashing ───────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260_000)
    return f"pbkdf2:sha256:260000${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, algo, rest = stored.split(":", 2)
        iters_str, salt, dk_hex = rest.split("$")
        dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt.encode("utf-8"), int(iters_str))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── User operations ────────────────────────────────────────────────────────

def _count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(name: str, surname: str, email: str, password: str) -> dict | None:
    """Create user. First user ever becomes admin. Returns user dict, or None if email taken."""
    role = "admin" if _count_users() == 0 else "user"
    user_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (id, name, surname, email, password_hash, role, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (user_id, name, surname, email, _hash_password(password), role, now),
            )
        return {"id": user_id, "name": name, "surname": surname, "email": email, "role": role}
    except sqlite3.IntegrityError:
        return None


def authenticate(email: str, password: str) -> dict | None:
    """Return user dict if credentials are valid, else None."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return {k: row[k] for k in ("id", "name", "surname", "email", "role")}


# ── Session operations ─────────────────────────────────────────────────────

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires),
        )
    return token


def get_session_user(token: str) -> dict | None:
    """Return user dict if session is valid and not expired."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT u.id, u.name, u.surname, u.email, u.role"
            " FROM sessions s JOIN users u ON s.user_id = u.id"
            " WHERE s.token = ? AND s.expires_at > ?",
            (token, datetime.now().isoformat()),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ── Password reset ─────────────────────────────────────────────────────────

def create_reset_token(email: str) -> str | None:
    """Create a time-limited reset token. Returns token, or None if email not found."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            return None
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(hours=RESET_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, row["id"], expires),
        )
    return token


def consume_reset_token(token: str, new_password: str) -> bool:
    """Validate token, update password, mark token used. Returns True on success."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM reset_tokens WHERE token = ? AND expires_at > ? AND used = 0",
            (token, datetime.now().isoformat()),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(new_password), row["user_id"]),
        )
        conn.execute("UPDATE reset_tokens SET used = 1 WHERE token = ?", (token,))
    return True


# ── Email ──────────────────────────────────────────────────────────────────

def send_reset_email(to_email: str, token: str) -> None:
    reset_url = f"{APP_URL}/set-password?token={token}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ChatKND — Redefinição de senha"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to_email
    text = (
        f"Você solicitou a redefinição de senha no ChatKND.\n\n"
        f"Clique no link para redefinir:\n{reset_url}\n\n"
        f"O link expira em {RESET_HOURS} hora(s). Se não foi você, ignore este email."
    )
    html = (
        f"<p>Você solicitou a redefinição de senha no <strong>ChatKND</strong>.</p>"
        f'<p><a href="{reset_url}">Clique aqui para redefinir sua senha</a></p>'
        f"<p>O link expira em {RESET_HOURS} hora(s).<br>"
        f"Se não foi você, ignore este email.</p>"
    )
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html",  "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, to_email, msg.as_string())
