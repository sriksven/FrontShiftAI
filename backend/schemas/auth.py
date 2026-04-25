"""
Authentication schemas
"""
from pydantic import BaseModel, EmailStr
from typing import Optional

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int  # access-token TTL in seconds
    company: Optional[str] = None
    email: str
    name: Optional[str] = None
    role: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutRequest(BaseModel):
    refresh_token: str

class UserInfo(BaseModel):
    email: str
    name: Optional[str] = None
    company: Optional[str] = None
    role: str

class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    company: Optional[str] = None
    role: str = "user"

class UpdatePasswordRequest(BaseModel):
    email: EmailStr
    new_password: str

class DeleteUserRequest(BaseModel):
    email: EmailStr

class BulkCreateUserRequest(BaseModel):
    users: list[CreateUserRequest]