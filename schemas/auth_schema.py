from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# ── Requests ───────────────────────────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email:        EmailStr
    otp:          str  = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    display_name: Optional[str] = Field(None, max_length=255)


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(..., description="Google ID token from Android Sign-In SDK")


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UpdateProfileRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)


class UpdateBudgetRequest(BaseModel):
    daily_budget:   Optional[float] = Field(None, ge=0)
    monthly_budget: Optional[float] = Field(None, ge=0)


# ── Responses ──────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id             : int
    email          : str
    display_name   : Optional[str]
    auth_provider  : str
    email_verified : bool
    daily_budget   : float
    monthly_budget : float
    created_at     : datetime

    model_config = {"from_attributes": True}


class SendOtpResponse(BaseModel):
    detail: str = "OTP sent to your email"


class LoginResponse(BaseModel):
    access_token  : str
    refresh_token : str
    token_type    : str = "bearer"
    is_new_user   : bool
    user          : UserResponse


class RefreshResponse(BaseModel):
    access_token: str
    token_type  : str = "bearer"


class LogoutResponse(BaseModel):
    detail: str = "Successfully logged out"


class MessageResponse(BaseModel):
    detail: str
