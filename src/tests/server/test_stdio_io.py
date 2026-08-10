from pytest_mock import MockerFixture

import ovid_core.server.stdio as stdio
from ovid_core.server import (
    AuthorizationResult,
    RequestContext,
    ServerConfig,
    StdioInitializeRequest,
    create_stdio_server,
)
from tests.server.server_helpers import build_registration


async def test_stdio_launcher_uses_bounded_binary_streams(mocker: MockerFixture) -> None:
    async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
        return AuthorizationResult(allowed=True)

    config = ServerConfig()
    server = create_stdio_server(agents=(await build_registration(),), authorize=authorize, config=config)
    request = f'{StdioInitializeRequest(request_id="init").model_dump_json()}\n'.encode()
    stdin = mocker.Mock()
    stdin.buffer.readline.side_effect = (request, b'')
    stdout = mocker.Mock()
    mocker.patch.object(stdio.sys, 'stdin', stdin)
    mocker.patch.object(stdio.sys, 'stdout', stdout)

    await server.run()

    assert stdin.buffer.readline.call_count == 2
    stdin.buffer.readline.assert_called_with(config.max_body_bytes + 1)
    stdout.buffer.write.assert_called_once()
    stdout.buffer.flush.assert_called_once_with()
