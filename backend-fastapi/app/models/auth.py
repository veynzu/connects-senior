from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=4096)


class AuthenticatedUser(BaseModel):
    uid: str
    email: str | None = None
    email_verified: bool | None = None
    display_name: str | None = None
    photo_url: str | None = None


class LoginResponse(BaseModel):
    id_token: str
    refresh_token: str
    expires_in: int
    user: AuthenticatedUser
