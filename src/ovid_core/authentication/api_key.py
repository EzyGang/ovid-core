from pydantic import SecretStr

from ovid_core.authentication.contracts import APIKeyCredentialBinding, AuthenticationFlowSession
from ovid_core.authentication.models import (
    AuthenticationInteraction,
    CompletedAuthenticationInteraction,
    InputRequestedAuthenticationInteraction,
)
from ovid_core.errors import AuthenticationError


class APIKeyAuthenticationFlow:
    id: str = 'api-key'
    label: str = 'API key'

    def __init__(self, *, binding: APIKeyCredentialBinding) -> None:
        self._binding = binding

    async def create_session(self) -> AuthenticationFlowSession:
        return _APIKeyAuthenticationSession(binding=self._binding)


class _APIKeyAuthenticationSession:
    def __init__(self, *, binding: APIKeyCredentialBinding) -> None:
        self._binding = binding
        self._started = False
        self._complete = False

    async def start(self) -> AuthenticationInteraction:
        if self._started:
            raise AuthenticationError('Authentication session already started')
        self._started = True
        return InputRequestedAuthenticationInteraction(input_kind='api_key')

    async def poll(self) -> AuthenticationInteraction | None:
        return None

    async def submit(self, value: SecretStr) -> AuthenticationInteraction:
        if not self._started or self._complete:
            raise AuthenticationError('Authentication session is not waiting for input')
        if not value.get_secret_value():
            raise AuthenticationError('API key must not be empty')
        await self._binding.save_api_key(value)
        self._complete = True
        return CompletedAuthenticationInteraction()

    async def cancel(self) -> None:
        self._complete = True
