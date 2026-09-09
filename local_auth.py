"""Local account authentication with opaque, server-side sessions."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from psycopg.rows import dict_row

from db_store import database_url


SESSION_COOKIE_NAME = "rag_session"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)
_DUMMY_HASH = _PASSWORD_HASHER.hash("not-a-real-password-for-timing-equality")


class RegistrationError(ValueError):
    pass


class InvalidCredentials(ValueError):
    pass


def normalize_email(value: str) -> str:
    email = unicodedata.normalize("NFKC", value).strip().casefold()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise RegistrationError("Укажите корректный адрес электронной почты.")
    return email


def validate_password(password: str, email: str = "") -> None:
    if len(password) < 12:
        raise RegistrationError("Пароль должен содержать не менее 12 символов.")
    if len(password) > 128:
        raise RegistrationError("Пароль не должен быть длиннее 128 символов.")
    local_part = email.partition("@")[0]
    if local_part and len(local_part) >= 4 and local_part in password.casefold():
        raise RegistrationError("Пароль не должен содержать адрес электронной почты.")


def _display_name(value: str, email: str) -> str:
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    if not name:
        name = email.partition("@")[0]
    if not 2 <= len(name) <= 80:
        raise RegistrationError("Имя должно содержать от 2 до 80 символов.")
    return name


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _auth_secret() -> bytes:
    value = os.getenv("RAG_SECRET_KEY", "").encode("utf-8")
    if len(value) < 32:
        raise RuntimeError("RAG_SECRET_KEY must contain at least 32 bytes")
    return value


def _session_lifetime() -> timedelta:
    hours = max(1, min(int(os.getenv("RAG_SESSION_LIFETIME_HOURS", "168")), 24 * 30))
    return timedelta(hours=hours)


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": str(row["email"]),
        "display_name": str(row["display_name"]),
        "is_admin": bool(row.get("is_admin", False)),
        "created_at": row.get("created_at"),
    }


def register_user(email: str, display_name: str, password: str) -> dict[str, Any]:
    normalized = normalize_email(email)
    validate_password(password, normalized)
    name = _display_name(display_name, normalized)
    password_hash = _PASSWORD_HASHER.hash(password)
    admin_emails = {
        item.strip().casefold()
        for item in os.getenv("RAG_LOCAL_ADMIN_EMAILS", "").split(",")
        if item.strip()
    }
    try:
        with psycopg.connect(database_url(), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_users (email, email_normalized, display_name, password_hash, is_admin)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (normalized, normalized, name, password_hash, normalized in admin_emails),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise RegistrationError("Не удалось создать аккаунт. Проверьте данные или выполните вход.") from exc
    if row is None:
        raise RuntimeError("User registration did not return a record")
    return dict(row)


def authenticate_credentials(email: str, password: str) -> dict[str, Any]:
    try:
        normalized = normalize_email(email)
    except RegistrationError:
        normalized = "invalid@example.invalid"
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM app_users WHERE email_normalized = %s AND active IS TRUE", (normalized,))
            row = cur.fetchone()

    candidate_hash = str(row["password_hash"]) if row else _DUMMY_HASH
    verified = False
    try:
        verified = _PASSWORD_HASHER.verify(candidate_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        verified = False

    now = datetime.now(timezone.utc)
    if row and row.get("locked_until") and row["locked_until"] > now:
        verified = False

    if not row or not verified:
        if row:
            lock_minutes = max(1, min(int(os.getenv("RAG_LOGIN_LOCK_MINUTES", "15")), 240))
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app_users
                        SET failed_login_count = failed_login_count + 1,
                            locked_until = CASE
                                WHEN failed_login_count + 1 >= 5 THEN now() + (%s * interval '1 minute')
                                ELSE locked_until
                            END,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (lock_minutes, row["id"]),
                    )
        raise InvalidCredentials("Неверная почта или пароль.")

    if _PASSWORD_HASHER.check_needs_rehash(candidate_hash):
        replacement_hash = _PASSWORD_HASHER.hash(password)
    else:
        replacement_hash = candidate_hash
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app_users
                SET password_hash = %s, failed_login_count = 0, locked_until = NULL,
                    last_login_at = now(), updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (replacement_hash, row["id"]),
            )
            refreshed = cur.fetchone()
    return dict(refreshed or row)


def create_session(user_id: str, user_agent: str = "", ip_address: str | None = None) -> str:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + _session_lifetime()
    agent_hash = hashlib.sha256(user_agent.encode("utf-8", errors="ignore")).hexdigest() if user_agent else ""
    try:
        normalized_ip = str(ipaddress.ip_address(ip_address)) if ip_address else None
    except ValueError:
        normalized_ip = None
    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_sessions (token_hash, user_id, expires_at, user_agent_hash, ip_address)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (_session_hash(token), user_id, expires_at, agent_hash, normalized_ip),
            )
            cur.execute(
                "DELETE FROM auth_sessions WHERE expires_at < now() OR revoked_at < now() - interval '7 days'"
            )
    return token


def authenticate_session(token: str) -> dict[str, Any]:
    if not token or len(token) > 256:
        raise InvalidCredentials("Требуется вход в систему.")
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.*
                FROM auth_sessions s
                JOIN app_users u ON u.id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.active IS TRUE
                """,
                (_session_hash(token),),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE auth_sessions SET last_seen_at = now()
                    WHERE token_hash = %s AND last_seen_at < now() - interval '5 minutes'
                    """,
                    (_session_hash(token),),
                )
    if row is None:
        raise InvalidCredentials("Требуется вход в систему.")
    return dict(row)


def revoke_session(token: str) -> None:
    if not token:
        return
    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
                (_session_hash(token),),
            )


def csrf_token(session_token: str) -> str:
    return hmac.new(_auth_secret(), f"csrf:{session_token}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(session_token: str, candidate: str) -> bool:
    return bool(session_token and candidate) and hmac.compare_digest(csrf_token(session_token), candidate)


def cookie_max_age() -> int:
    return int(_session_lifetime().total_seconds())


def cookie_secure() -> bool:
    return os.getenv("RAG_SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
