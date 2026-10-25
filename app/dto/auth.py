
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
    status: Optional[str] = Field(None, description="Account status: 'pending' until an administrator approves it",
                                  examples=["pending"])


class AdminUser(BaseModel):
    id: int
    username: str
    email: str
    status: str = Field(..., description="pending | approved | rejected | disabled")
    role: str = Field('user', description="user | admin")
    is_admin: bool = False
    is_owner: bool = Field(False, description="Listed in ADMIN_EMAILS: always an admin, cannot be changed here")
    created_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[int] = None


class AdminUserList(BaseModel):
    users: list[AdminUser] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict, description="Accounts per status, for the tab badges")
    total: int = 0
    pages: int = 0
    current_page: int = 1
    per_page: int = 100


class AdminUserQuery(BaseModel):
    status: Optional[str] = Field(None, pattern='^(pending|approved|rejected|disabled)$',
                                  description="Only accounts with this status")
    q: Optional[str] = Field(None, max_length=100, description="Search username or email")


class AdminUserPath(BaseModel):
    user_id: int = Field(..., description="Account id")


class AdminRoleRequest(BaseModel):
    role: str = Field(..., pattern='^(user|admin)$', description="user | admin")


class AdminReviewRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500, description="Optional note sent to the user on rejection")
    notify: bool = Field(True, description="Email the user about the decision (when email is configured)")


class LoginRequest(BaseModel):
    username: str = Field(..., description="Username", examples=["johndoe"])
    password: str = Field(..., description="Password", examples=["securePass123"])


class CreateTokenRequest(BaseModel):
    expires_in_days: Optional[int] = Field(default=None, description="Token TTL in days. Defaults to server's TOKEN_TTL_DAYS.", examples=[30])


class LoginResponse(BaseModel):
    msg: str = Field(..., description="Login result message", examples=["login success"])
    access_token: str = Field(..., description="Bearer token for authenticated requests", examples=["a1b2c3d4..."])
