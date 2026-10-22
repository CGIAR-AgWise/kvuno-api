
from typing import Optional
from pydantic import BaseModel, Field, field_validator

# bcrypt only uses the first 72 bytes of a password. bcrypt < 5 silently
# truncated; bcrypt 5.x raises ValueError instead, which turned an over-long
# password into a 500 at registration. The limit is in BYTES of the UTF-8
# encoding, not characters — 72 characters of non-ASCII text can be ~288 bytes
# — so it needs a validator rather than max_length.
BCRYPT_MAX_PASSWORD_BYTES = 72


def password_too_long(password: str) -> bool:
    return len(password.encode('utf-8')) > BCRYPT_MAX_PASSWORD_BYTES


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100, description="Desired username", examples=["johndoe"])
    email: str = Field(..., description="Email address", examples=["john@example.com"])
    password: str = Field(..., min_length=8,
                          description=f"Password (at most {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded)",
                          examples=["securePass123"])

    @field_validator('password')
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if password_too_long(value):
            raise ValueError(
                f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded."
            )
        return value


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
