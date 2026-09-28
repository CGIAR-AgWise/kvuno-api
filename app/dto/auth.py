
from typing import Optional
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100, description="Desired username", examples=["johndoe"])
    email: str = Field(..., description="Email address", examples=["john@example.com"])
    password: str = Field(..., min_length=8, description="Password", examples=["securePass123"])


class RegisterResponse(BaseModel):
    msg: str = Field(..., description="Registration result message", examples=["registration success"])


class LoginRequest(BaseModel):
    username: str = Field(..., description="Username", examples=["johndoe"])
    password: str = Field(..., description="Password", examples=["securePass123"])


class CreateTokenRequest(BaseModel):
    expires_in_days: Optional[int] = Field(default=None, description="Token TTL in days. Defaults to server's TOKEN_TTL_DAYS.", examples=[30])


class LoginResponse(BaseModel):
    msg: str = Field(..., description="Login result message", examples=["login success"])
    access_token: str = Field(..., description="Bearer token for authenticated requests", examples=["a1b2c3d4..."])
