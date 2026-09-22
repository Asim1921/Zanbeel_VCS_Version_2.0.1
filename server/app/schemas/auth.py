"""Request bodies for authentication and registration endpoints."""

from typing import Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str

class BootstrapPasswordRequest(BaseModel):
    username: str
    new_password: str
    setup_key: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordWithOtpRequest(BaseModel):
    email: str
    otp: str
    new_password: str

class RegistrationRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    requested_role: str = 'developer'
    requested_team_lead_username: Optional[str] = None

class ReviewRegistrationRequest(BaseModel):
    action: str  # approve or reject
    comment: Optional[str] = None
    role: Optional[str] = None
    team_lead_id: Optional[int] = None
