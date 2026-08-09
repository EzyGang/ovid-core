from typing import Literal

from pydantic import Field, SecretStr

from ovid_core.models import BaseModel


class CodexOAuthConfig(BaseModel):
    issuer: str = Field(default='https://auth.openai.com', min_length=1)
    client_id: str = Field(default='app_EMoamEEZ73f0CkXaXp7hrann', min_length=1)
    backend_url: str = Field(default='https://chatgpt.com/backend-api/codex', min_length=1)
    poll_timeout_seconds: float = Field(default=900, gt=0)
    refresh_window_seconds: float = Field(default=300, ge=0)


class CodexDeviceAuthorization(BaseModel):
    verification_url: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    device_auth_id: SecretStr = Field(repr=False, exclude=True)
    interval_seconds: float = Field(gt=0, repr=False, exclude=True)


class CodexTokens(BaseModel):
    id_token: SecretStr = Field(repr=False)
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)


class _UserCodeRequest(BaseModel):
    client_id: str = Field(min_length=1)


class _DeviceTokenRequest(BaseModel):
    device_auth_id: str = Field(min_length=1, repr=False)
    user_code: str = Field(min_length=1, repr=False)


class _AuthorizationCodeExchangeRequest(BaseModel):
    grant_type: Literal['authorization_code'] = 'authorization_code'
    code: str = Field(min_length=1, repr=False)
    redirect_uri: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    code_verifier: str = Field(min_length=1, repr=False)


class _UserCodeResponse(BaseModel):
    device_auth_id: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    interval: str | float | int


class _DeviceTokenResponse(BaseModel):
    authorization_code: str = Field(min_length=1)
    code_verifier: str = Field(min_length=1)


class _OAuthTokenResponse(BaseModel):
    id_token: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr
