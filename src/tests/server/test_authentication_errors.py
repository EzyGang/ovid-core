import pytest
from pydantic import JsonValue
from pydantic_ai.exceptions import ModelHTTPError
from pytest_mock import MockerFixture

from ovid_core.adapters.pydantic_ai._agent_errors import normalize_run_error
from ovid_core.errors import AuthenticationError, CredentialError
from ovid_core.server import (
    AuthorizationResult,
    CommandRegistration,
    RequestContext,
    StdioCommandRequest,
    create_stdio_server,
)
from tests.server.test_stdio_server import _frame, _payloads, _serve_frames


@pytest.mark.parametrize(
    ('error', 'code', 'message'),
    [
        (CredentialError('credential-secret'), 'credential_error', 'Provider credentials are unavailable'),
        (AuthenticationError('authentication-secret'), 'authentication_error', 'Provider authentication failed'),
        (
            ModelHTTPError(401, 'model', {'error': 'provider-secret'}),
            'authentication_error',
            'Provider authentication failed',
        ),
        (
            ModelHTTPError(503, 'model', {'error': 'provider-secret'}),
            'command_failed',
            'Command execution failed',
        ),
    ],
)
async def test_command_errors_keep_safe_classification_and_connection_usable(
    mocker: MockerFixture,
    error: Exception,
    code: str,
    message: str,
) -> None:
    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    async def command(_: RequestContext, __: AuthorizationResult, arguments: JsonValue) -> JsonValue:
        if arguments == 'fail':
            raise normalize_run_error(error)
        return 'recovered'

    server = create_stdio_server(
        agents=(),
        commands=(CommandRegistration(id='state', description='Read state.', handler=command),),
        authorize=authorize,
    )
    writer = await _serve_frames(
        server,
        mocker,
        'success',
        (
            _frame(StdioCommandRequest(request_id='failure', command_id='state', arguments='fail')),
            _frame(StdioCommandRequest(request_id='success', command_id='state')),
        ),
    )
    payloads = {payload['request_id']: payload for payload in _payloads(writer)}
    assert payloads['failure']['error'] == {'code': code, 'message': message}
    assert payloads['success']['result'] == 'recovered'
