import json

import pytest
from pydantic import ValidationError

from ovid_core.journal import ConversationJournal, RunStartedEntry
from ovid_core.runtime import ConversationId, RunId
from ovid_core.server import AgentRunRequest, ServerConfig, ServerErrorResponse
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


def test_client_request_id_length_and_old_journal_entries_without_it() -> None:
    conversation_id = ConversationId.new()
    entry = RunStartedEntry.model_validate({'run_id': str(RunId.new()), 'at': '2026-01-01T00:00:00Z'})

    journal = ConversationJournal.from_entries((), (entry,))

    assert AgentRunRequest(prompt='Go.', client_request_id='k' * 128).client_request_id == 'k' * 128
    for key in ('', 'k' * 129):
        with pytest.raises(ValidationError):
            AgentRunRequest(prompt='Go.', conversation_id=conversation_id, client_request_id=key)
    assert (entry.client_request_id, journal.runs[0].client_request_id) == (None, None)


def test_an_error_without_a_run_id_keeps_its_payload() -> None:
    error = ServerErrorResponse(code='timeout', message='Request timed out')

    assert json.loads(error.model_dump_json()) == {'code': 'timeout', 'message': 'Request timed out'}
