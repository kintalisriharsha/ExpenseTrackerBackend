from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Requests ───────────────────────────────────────────────────────────────────

class FirebaseLoginRequest(BaseModel):
    id_token: str = Field(..., description="Firebase ID token from Android SDK")


class RegisterRequest(BaseModel):
    """
    POST /auth/register — called after OTP verification on the signup flow.

    Android sends all 4 fields once Firebase OTP is confirmed:
        id_token       → Firebase ID token (proves phone ownership)
        display_name   → Full name from SignUpScreen
        daily_budget   → Can be 0.0 if user skips budget setup
        monthly_budget → Can be 0.0 if user skips budget setup
    """
    id_token       : str   = Field(...,  description="Firebase ID token from OTP verification")
    display_name   : str   = Field(...,  min_length=1, max_length=255)
    daily_budget   : float = Field(0.0, ge=0.0)
    monthly_budget : float = Field(0.0, ge=0.0)


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., description="The refresh token to invalidate")


# ── Responses ──────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id             : int
    phone_number   : str
    display_name   : Optional[str]
    daily_budget   : float
    monthly_budget : float
    created_at     : datetime

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token  : str
    refresh_token : str
    token_type    : str = "bearer"
    is_new_user   : bool
    user          : UserResponse


class RegisterResponse(BaseModel):
    access_token  : str
    refresh_token : str
    token_type    : str = "bearer"
    user          : UserResponse


class RefreshResponse(BaseModel):
    access_token : str
    token_type   : str = "bearer"


class LogoutResponse(BaseModel):
    detail: str = "Successfully logged out"