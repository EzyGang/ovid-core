import asyncio
import sys
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from ovid_core.server import AgentRunRequest, StdioCommandRequest, StdioInitializeRequest, StdioRunRequest


_SERVER_PATH = Path(__file__).with_name('stdio_agent_server.py')
_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])


async def test_versioned_stdio_subprocess_streams_agent_and_command_results() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_SERVER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    requests = (
        StdioInitializeRequest(request_id='initialize'),
        StdioRunRequest(request_id='run', agent_id='writer', request=AgentRunRequest(prompt='Write.')),
        StdioCommandRequest(request_id='command', command_id='inspect', arguments='state'),
    )
    stdin = ''.join(f'{request.model_dump_json()}\n' for request in requests).encode()

    assert process.stdin is not None
    assert process.stdout is not None
    payloads: list[dict[str, JsonValue]] = []
    try:
        async with asyncio.timeout(10):
            process.stdin.write(stdin)
            await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                assert line, 'Server exited before the command result'
                payload = _PAYLOAD_ADAPTER.validate_json(line)
                payloads.append(payload)
                if payload['type'] == 'command_result':
                    break
            stdout, stderr = await process.communicate()
            assert stdout == b''
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    run_payloads = [payload for payload in payloads if payload['request_id'] == 'run']

    assert process.returncode == 0
    assert stderr == b''
    assert payloads[0]['type'] == 'initialized'
    assert [payload['type'] for payload in run_payloads][-1] == 'run_result'
    assert any(payload.get('event', {}).get('kind') == 'text_delta' for payload in run_payloads)
    assert payloads[-1] == {
        'version': 1,
        'request_id': 'command',
        'type': 'command_result',
        'result': {'arguments': 'state', 'method': 'STDIO', 'principal': 'subprocess'},
    }
