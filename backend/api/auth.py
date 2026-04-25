from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import timedelta, datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
import hashlib
import secrets
import jwt
import os

from schemas import (
    LoginRequest, LoginResponse, UserInfo,
    RefreshRequest, RefreshResponse, LogoutRequest,
)
from services import validate_credentials
from db import get_db
from db.models import RefreshToken, User

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
security = HTTPBearer()

# JWT Configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("JWT_SECRET_KEY environment variable is not set")

ALGORITHM = "HS256"
# Phase 0.7A: short-lived access, long-lived revocable refresh.
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour
REFRESH_TOKEN_EXPIRE_DAYS = 30
# Phase 0.7G: voice worker's lifetime (<=1h session, 2h LiveKit TTL) fits
# inside a single voice-scoped access token, so the worker never needs a
# refresh flow itself. 6h gives generous headroom for long sessions.
VOICE_TOKEN_EXPIRE_MINUTES = 60 * 6


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        company: str = payload.get("company")
        role: str = payload.get("role")
        name: str = payload.get("name")
        if email is None or role is None:
            return None
        return {"email": email, "company": company, "role": role, "name": name}
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except (jwt.PyJWTError, jwt.DecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )


def _hash_token(raw: str) -> str:
    """SHA-256 hash of a refresh token secret. Never store raw values."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_refresh_token(
    db: Session,
    user_email: str,
    company: Optional[str],
    rotated_from: Optional[str] = None,
) -> str:
    """Mint a new refresh token and persist its hash. Return the raw secret.

    Caller returns the raw secret to the client exactly once; the DB only
    ever holds the hash, so a DB dump doesn't yield usable tokens.
    """
    raw = secrets.token_urlsafe(48)
    record = RefreshToken(
        user_email=user_email,
        company=company,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        rotated_from=rotated_from,
    )
    db.add(record)
    db.commit()
    return raw


def _revoke_chain(db: Session, start_token_id: str) -> None:
    """Revoke every token that descended from ``start_token_id`` inclusive.

    Theft detection: if a revoked token is ever presented again, the whole
    rotation chain is burned so an attacker holding an older copy is evicted.
    """
    now = datetime.now(timezone.utc)
    # Walk forward through the rotation chain.
    frontier = {start_token_id}
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        tokens = db.query(RefreshToken).filter(
            (RefreshToken.id == current) | (RefreshToken.rotated_from == current)
        ).all()
        for tok in tokens:
            if tok.revoked_at is None:
                tok.revoked_at = now
            if tok.id != current and tok.id not in seen:
                frontier.add(tok.id)
    db.commit()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    user_data = decode_access_token(token)
    if user_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )
    return user_data


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Validate credentials and return a short-lived access token + a refresh token."""
    try:
        is_valid, company, role, name = validate_credentials(request.email, request.password, db)

        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": request.email, "company": company, "role": role, "name": name},
            expires_delta=access_token_expires
        )
        refresh_token = _issue_refresh_token(db, user_email=request.email, company=company)

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            company=company,
            email=request.email,
            role=role,
            name=name,
        )

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/refresh", response_model=RefreshResponse)
def refresh(request: RefreshRequest, db: Session = Depends(get_db)):
    """Rotate the refresh token.

    On success: revoke the presented token and mint a fresh access + refresh
    pair linked via ``rotated_from``. If the presented token is already
    revoked, treat it as a replay/theft attempt — revoke the whole chain.
    """
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_token(request.refresh_token))
        .first()
    )

    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    # Replay of a revoked token → burn the chain.
    if record.revoked_at is not None:
        _revoke_chain(db, record.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token reuse detected; chain revoked",
        )

    # Ensure expires_at is timezone-aware for comparison (SQLite may return naive).
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    # Look up the user record to embed up-to-date name/role in the new access token.
    user = db.query(User).filter(User.email == record.user_email).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")

    # Revoke old, issue new (rotation-on-use).
    record.revoked_at = datetime.now(timezone.utc)
    db.commit()

    new_refresh = _issue_refresh_token(
        db,
        user_email=record.user_email,
        company=record.company,
        rotated_from=record.id,
    )
    new_access = create_access_token(
        data={
            "sub": user.email,
            "company": user.company,
            "role": user.role.value if hasattr(user.role, "value") else str(user.role),
            "name": user.name,
        },
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout")
def logout(request: LogoutRequest, db: Session = Depends(get_db)):
    """Revoke the refresh token. Idempotent — absent/invalid tokens still 200."""
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_token(request.refresh_token))
        .first()
    )
    if record and record.revoked_at is None:
        record.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return {"status": "logged out"}


@router.post("/voice-token")
async def voice_token(current_user: dict = Depends(get_current_user)):
    """Mint a voice-scoped access token (6h TTL) for the voice worker.

    Phase 0.7G: the voice worker sits inside a LiveKit session that can last
    up to ~2h. Handing it a regular 1h access token means it would start
    failing auth mid-conversation, and we don't want to build refresh-flow
    machinery inside the worker. This endpoint returns a longer-lived token
    scoped with ``scope=voice`` so it's distinguishable from normal access
    tokens in logs and can be denied at sensitive endpoints later if needed.
    """
    access_token = create_access_token(
        data={
            "sub": current_user["email"],
            "company": current_user.get("company"),
            "role": current_user["role"],
            "name": current_user.get("name"),
            "scope": "voice",
        },
        expires_delta=timedelta(minutes=VOICE_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": VOICE_TOKEN_EXPIRE_MINUTES * 60,
        "scope": "voice",
    }


@router.get("/me", response_model=UserInfo)
async def get_user_info(current_user: dict = Depends(get_current_user)):
    """Get current user information from token"""
    return UserInfo(
        email=current_user["email"],
        company=current_user.get("company"),
        role=current_user["role"],
        name=current_user.get("name")
    )