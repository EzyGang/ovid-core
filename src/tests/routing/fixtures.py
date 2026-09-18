import json
from typing import Any

import httpx


def model_reply(content: str = 'done', *, tool_value: int | None = None) -> httpx.Response:
    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if tool_value is not None:
        message = {
            'role': 'assistant',
            'tool_calls': [
                {
                    'id': f'call-{tool_value}',
                    'type': 'function',
                    'function': {'name': 'add', 'arguments': json.dumps({'left': tool_value, 'right': 0})},
                }
            ],
        }
    return httpx.Response(
        200,
        headers={'set-cookie': 'session=retained; Path=/'},
        json={
            'id': 'reply',
            'object': 'chat.completion',
            'created': 0,
            'model': 'gpt-4o',
            'choices': [
                {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if tool_value is not None else 'stop'}
            ],
        },
    )
