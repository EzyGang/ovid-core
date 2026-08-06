from uuid import UUID

from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import RequestUsage


RUN_ID = RunId(UUID('00000000-0000-0000-0000-000000000001'))
CONVERSATION_ID = ConversationId(UUID('00000000-0000-0000-0000-000000000002'))


def make_request_usage(
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> RequestUsage:
    return RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
