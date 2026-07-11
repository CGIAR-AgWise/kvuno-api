import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import bcrypt
from flask import request
from flask_openapi3 import Tag, APIBlueprint
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import API_PREFIX, API_VERSION, RATE_LIMIT_REGISTER, RATE_LIMIT_LOGIN, TOKEN_TTL_DAYS
from app.dto.auth import RegisterRequest, RegisterResponse, LoginRequest, LoginResponse, CreateTokenRequest
from app.models.database_conn import MyDb
from app.models.kvuno import User, UserToken
from app.rate_limit import limiter

__bp__ = "/users"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name='User', description="User management API")

api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag])


def _format_token(token_id: int, secret: str) -> str:
    """'{id}|{secret}' — the visible prefix lets users identify which token they use."""
    return f"{token_id}|{secret}"


def _parse_token(raw: str) -> tuple[int, str] | None:
    """Split '{id}|{secret}' back into (id, secret). Returns None on bad format."""
    if '|' not in raw:
        return None
    try:
        tid, secret = raw.split('|', 1)
        return int(tid), secret
    except (ValueError, IndexError):
        return None


def _hash_token(token_id: int, secret: str) -> str:
    return hashlib.sha256(f"{token_id}|{secret}".encode()).hexdigest()


@api.post('/register',
          responses={201: RegisterResponse, 409: {"description": "Username or email already exists"}},
          summary="Register a new user account",
          description="Create a new user with username, email, and password.",
          security=[])
@limiter.limit(RATE_LIMIT_REGISTER)
def register(body: RegisterRequest):
    db = MyDb.get_db()
    existing = db.session.query(User).filter(
        (User.username == body.username) | (User.email == body.email)
    ).first()
    if existing:
        return {"msg": "username or email already exists"}, 409

    password_hash = bcrypt.hashpw(body.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user = User(username=body.username, email=body.email, password_hash=password_hash)
    db.session.add(user)
    db.session.commit()
    return {"msg": "registration success"}, 201


@api.post('/login',
          responses={200: LoginResponse, 401: {"description": "Invalid credentials"}},
          summary="Authenticate and get a Bearer token",
          description="Exchange valid credentials for a JWT access token (id|secret format). Token TTL is configurable via TOKEN_TTL_DAYS.",
          security=[])
@limiter.limit(RATE_LIMIT_LOGIN)
def login(body: LoginRequest):
    db = MyDb.get_db()
    user = db.session.query(User).filter(User.username == body.username).first()
    if not user or not bcrypt.checkpw(body.password.encode('utf-8'), user.password_hash.encode('utf-8')):
        return {"msg": "invalid credentials"}, 401

    secret = secrets.token_hex(32)
    token_id = db.session.execute(
        text("INSERT INTO user_tokens (user_id, token, expires_at, created_at) "
             "VALUES (:uid, '', :exp, now()) RETURNING id"),
        {"uid": user.id, "exp": None if TOKEN_TTL_DAYS <= 0
         else datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)},
    ).scalar()
    token_hash = _hash_token(token_id, secret)
    db.session.execute(
        text("UPDATE user_tokens SET token = :hash WHERE id = :id"),
        {"hash": token_hash, "id": token_id},
    )
    db.session.commit()

    return {"msg": "login success", "access_token": _format_token(token_id, secret)}, 200


def _resolve_token(raw: str):
    """Look up a ``{id}|{secret}`` token and return the matching User, or None."""
    parsed = _parse_token(raw)
    if parsed is None:
        return None
    token_id, secret = parsed
    db = MyDb.get_db()
    token_record = db.session.query(UserToken).filter(UserToken.id == token_id).first()
    if not token_record:
        return None
    if token_record.token != _hash_token(token_id, secret):
        return None
    if token_record.expires_at is not None and token_record.expires_at < datetime.now(timezone.utc):
        return None
    return db.session.query(User).filter(User.id == token_record.user_id).first()


def get_current_user():
    """Extract the authenticated user from the request.

    Checks (in order):
      1. ``Authorization: Bearer <token>`` header
      2. ``token`` cookie

    Returns:
        User or None if not authenticated.
    """
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        raw = auth_header.replace('Bearer ', '', 1)
        user = _resolve_token(raw)
        if user:
            return user

    cookie = request.cookies.get('token')
    if cookie:
        user = _resolve_token(unquote(cookie))
        if user:
            return user

    return None


@api.post('/logout',
          responses={200: {"description": "Logged out"}, 401: {"description": "Invalid or missing token"}},
          summary="Revoke the current token",
          description="Delete the current Bearer token from the database. Caller should also clear the client-side token cookie.",
          security=[{"jwt": []}])
class TokenInfoResponse(BaseModel):
    id: int = Field(..., description="Token ID")
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: str | None = Field(None, description="Expiration timestamp")


class TokenListResponse(BaseModel):
    tokens: list[TokenInfoResponse] = Field(default=[], description="Active tokens")


@api.get('/tokens',
         responses={200: TokenListResponse, 401: {"description": "Authentication required"}},
         summary="List active tokens for the current user",
         description="Return all non-expired tokens belonging to the authenticated user.",
         security=[{"jwt": []}])
def list_tokens():
    user = get_current_user()
    if not user:
        return {"msg": "authentication required"}, 401
    db = MyDb.get_db()
    now = datetime.now(timezone.utc)
    tokens = db.session.query(UserToken).filter(
        UserToken.user_id == user.id,
        (UserToken.expires_at.is_(None)) | (UserToken.expires_at > now),
    ).all()
    return TokenListResponse(
        tokens=[
            TokenInfoResponse(
                id=t.id,
                created_at=t.created_at.isoformat() if t.created_at else "",
                expires_at=t.expires_at.isoformat() if t.expires_at else None,
            )
            for t in tokens
        ]
    ).model_dump(), 200


@api.post('/tokens',
          responses={201: LoginResponse, 401: {"description": "Authentication required"}},
          summary="Create a new API token",
          description="Generate a new token for the authenticated user.",
          security=[{"jwt": []}])
def create_token(body: CreateTokenRequest):
    user = get_current_user()
    if not user:
        return {"msg": "authentication required"}, 401
    _db = MyDb.get_db()
    secret = secrets.token_hex(32)
    ttl = body.expires_in_days if body.expires_in_days is not None else TOKEN_TTL_DAYS
    token_id = _db.session.execute(
        text("INSERT INTO user_tokens (user_id, token, expires_at, created_at) "
             "VALUES (:uid, '', :exp, now()) RETURNING id"),
        {"uid": user.id, "exp": None if ttl <= 0
         else datetime.now(timezone.utc) + timedelta(days=ttl)},
    ).scalar()
    token_hash = _hash_token(token_id, secret)
    _db.session.execute(
        text("UPDATE user_tokens SET token = :hash WHERE id = :id"),
        {"hash": token_hash, "id": token_id},
    )
    _db.session.commit()
    return {"msg": "token created", "access_token": _format_token(token_id, secret)}, 201


@api.delete('/tokens/<int:token_id>',
            responses={200: {"description": "Token revoked", "content": {"application/json": {"schema": {"type": "object", "properties": {"msg": {"type": "string"}}}}}}, 401: {"description": "Authentication required"},
                       404: {"description": "Token not found"}},
            summary="Revoke a specific token by ID",
            description="Delete a token belonging to the current user.",
            security=[{"jwt": []}])
def revoke_token(token_id: int):
    user = get_current_user()
    if not user:
        return {"msg": "authentication required"}, 401
    db = MyDb.get_db()
    token = db.session.query(UserToken).filter(
        UserToken.id == token_id, UserToken.user_id == user.id
    ).first()
    if not token:
        return {"msg": "token not found"}, 404
    db.session.delete(token)
    db.session.commit()
    return {"msg": "token revoked"}, 200


def logout():
    """Revoke the current token by deleting it from user_tokens."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return {"msg": "no token provided"}, 401
    raw = auth_header.replace('Bearer ', '', 1)
    parsed = _parse_token(raw)
    if parsed is None:
        return {"msg": "invalid token"}, 401
    token_id, secret = parsed
    db = MyDb.get_db()
    token_record = db.session.query(UserToken).filter(UserToken.id == token_id).first()
    if not token_record or token_record.token != _hash_token(token_id, secret):
        return {"msg": "invalid token"}, 401
    db.session.delete(token_record)
    db.session.commit()

    resp = {"msg": "logged out successfully"}
    # Flask view can't delete cookies on a JSON response easily,
    # so the caller should also clear the client-side cookie.
    return resp, 200
