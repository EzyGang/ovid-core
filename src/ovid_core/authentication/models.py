from typing import Annotated, Literal

from pydantic import Field

from ovid_core.models import BaseModel, UUIDRootModel


type AuthenticationState = Literal['connected', 'disconnected', 'pending', 'failed']
type AuthenticationInputKind = Literal['api_key', 'callback_url', 'authorization_code']
type AuthenticationProgressStage = Literal['starting', 'waiting', 'exchanging']
type AuthenticationFailureKind = Literal[
    'rejected',
    'timeout',
    'provider_error',
    'invalid_response',
    'cancelled',
    'unknown',
]


class AuthenticationSessionId(UUIDRootModel):
    pass


class AuthenticationFlowDescriptor(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ProviderAuthentication(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: AuthenticationState
    flows: tuple[AuthenticationFlowDescriptor, ...] = Field(min_length=1)


class AuthenticationStatus(BaseModel):
    configured: bool
    providers: tuple[ProviderAuthentication, ...] = Field(min_length=1)


class InputRequestedAuthenticationInteraction(BaseModel):
    kind: Literal['input_requested'] = 'input_requested'
    input_kind: AuthenticationInputKind
    secret: bool = True
    optional: bool = False


class BrowserAuthorizationAuthenticationInteraction(BaseModel):
    kind: Literal['browser_authorization'] = 'browser_authorization'
    url: str = Field(min_length=1)
    manual_callback: bool = False


class DeviceAuthorizationAuthenticationInteraction(BaseModel):
    kind: Literal['device_authorization'] = 'device_authorization'
    url: str = Field(min_length=1)
    user_code: str = Field(min_length=1)


class ProgressAuthenticationInteraction(BaseModel):
    kind: Literal['progress'] = 'progress'
    stage: AuthenticationProgressStage


class CompletedAuthenticationInteraction(BaseModel):
    kind: Literal['completed'] = 'completed'


class FailedAuthenticationInteraction(BaseModel):
    kind: Literal['failed'] = 'failed'
    reason: AuthenticationFailureKind


type AuthenticationInteraction = Annotated[
    InputRequestedAuthenticationInteraction
    | BrowserAuthorizationAuthenticationInteraction
    | DeviceAuthorizationAuthenticationInteraction
    | ProgressAuthenticationInteraction
    | CompletedAuthenticationInteraction
    | FailedAuthenticationInteraction,
    Field(discriminator='kind'),
]


class AuthenticationSessionStarted(BaseModel):
    session_id: AuthenticationSessionId
    interaction: AuthenticationInteraction


class AuthenticationSessionUpdate(BaseModel):
    interaction: AuthenticationInteraction | None = None
