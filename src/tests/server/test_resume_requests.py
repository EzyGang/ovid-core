import pytest
from pydantic import ValidationError

from ovid_core.runtime import ConversationId
from ovid_core.server import AgentRunRequest, ServerConfig
from ovid_core.tools import ToolCallApproved


def test_resume_request_needs_a_conversation_and_excludes_prompt_and_answers() -> None:
    conversation_id = ConversationId.new()

    request = AgentRunRequest.model_validate_json(f'{{"resume": true, "conversation_id": "{conversation_id}"}}')

    assert (request.resume, request.prompt, request.answers) == (True, None, None)
    assert ServerConfig().resume_restart_limit == 3
    with pytest.raises(ValidationError, match='cannot carry a prompt or tool answers'):
        AgentRunRequest(resume=True, prompt='Go.', conversation_id=conversation_id)
    with pytest.raises(ValidationError, match='cannot carry a prompt or tool answers'):
        AgentRunRequest(resume=True, answers={'c1': ToolCallApproved()}, conversation_id=conversation_id)
    with pytest.raises(ValidationError, match='require a conversation ID'):
        AgentRunRequest(resume=True)
    with pytest.raises(ValidationError, match='requires a prompt, tool answers, or resume'):
        AgentRunRequest(resume=False, conversation_id=conversation_id)
