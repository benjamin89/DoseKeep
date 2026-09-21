"""Local account authentication and encrypted per-user integration settings."""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from .database import get_session
from .models import User


password_hash = PasswordHash.recommended()


def master_key() -> str:
    value = os.getenv("DOSEKEEP_MASTER_KEY", "")
    if not value:
        raise RuntimeError("DOSEKEEP_MASTER_KEY must be configured before starting DoseKeep")
    return value


def fernet() -> Fernet:
    digest = hashlib.sha256(master_key().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_config(config: dict) -> str:
    return fernet().encrypt(json.dumps(config).encode()).decode()


def decrypt_config(value: str) -> dict:
    return json.loads(fernet().decrypt(value.encode()).decode())


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, value: str) -> bool:
    return password_hash.verify(password, value)


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    return user
