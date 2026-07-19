"""Identity: anonymous-first, account optional.

Every visitor gets a random anonymous id in the signed session cookie on first
request; books belong to it. Registering or logging in binds the session to a
user row and *claims* the anonymous books, so nothing imported before signup is
lost. Passwords are bcrypt-hashed.
"""

from __future__ import annotations

import re
import uuid

import bcrypt
from fastapi import Request

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def ensure_anon(request: Request) -> str:
    """Anonymous identity for this browser; created on first touch."""
    anon = request.session.get("anon")
    if not anon:
        anon = uuid.uuid4().hex
        request.session["anon"] = anon
    return anon


def identity(request: Request) -> tuple[str | None, str]:
    """(user_id or None, anon_id) for the current session."""
    return request.session.get("user"), ensure_anon(request)


def owner_where(request: Request) -> tuple[str, list]:
    """SQL fragment + params selecting the current identity's books."""
    user_id, anon_id = identity(request)
    if user_id:
        return "user_id = %s", [user_id]
    return "anon_id = %s AND user_id IS NULL", [anon_id]
