import hashlib
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

import bcrypt


class AuthService:
    """SQLite-backed user accounts and shared, expiring sessions."""

    SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))

    def __init__(self):
        configured_path = os.getenv("AUTH_DB_PATH")
        self.db_path = (
            Path(configured_path) if configured_path
            else Path(__file__).resolve().parents[1] / "auth_data" / "users.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize_database(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    password_hash BLOB NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)"
            )

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_user(self, username, password):
        username = (username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", username):
            raise ValueError(
                "Username must be 3-32 characters and contain only letters, "
                "numbers, underscores, or hyphens."
            )
        if not isinstance(password, str) or len(password) < 8:
            raise ValueError("Password must contain at least 8 characters.")
        encoded = password.encode("utf-8")
        if len(encoded) > 72:
            raise ValueError("Password must be at most 72 UTF-8 bytes.")

        password_hash = bcrypt.hashpw(encoded, bcrypt.gensalt())
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, password_hash, time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("That username is already registered.") from exc

    def login(self, username, password):
        if not isinstance(password, str):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                ((username or "").strip(),),
            ).fetchone()

        if row is None:
            return None
        stored_hash = row["password_hash"]
        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("ascii")
        try:
            if not bcrypt.checkpw(password.encode("utf-8"), stored_hash):
                return None
        except (ValueError, TypeError):
            return None

        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO sessions(token_hash, username, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (self._token_hash(token), row["username"], now,
                 now + self.SESSION_TTL_SECONDS),
            )
        return token

    def logout(self, token):
        if not token:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),)
            )
            return cur.rowcount > 0

    def validate_token(self, token):
        if not token:
            return None
        digest = self._token_hash(token)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, expires_at FROM sessions WHERE token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
            return row["username"]
