from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from ovid_core.errors import (
    AgentConstructionError,
    ConfigurationError,
    CredentialError,
    ModelResolutionError,
    OvidCoreError,
    PluginError,
    ProviderError,
    ToolError,
    TransportError,
)
from ovid_core.runtime.context import RunContext
from ovid_core.runtime.events import (
    AgentEvent,
    ModelRequestStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageUpdateEvent,
)
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import Usage
from tests.helpers import CONVERSATION_ID, RUN_ID, make_request_usage


def test_identifiers_generate_validate_and_serialize() -> None:
    run_id = RunId.new()
    conversation_id = ConversationId.new()

    assert RunId.model_validate_json(run_id.model_dump_json()) == run_id
    assert ConversationId.model_validate_json(conversation_id.model_dump_json()) == conversation_id
    assert UUID(str(run_id)) == run_id.root
    assert UUID(str(conversation_id)) == conversation_id.root
    with pytest.raises(ValidationError):
        RunId('not-a-uuid')


def test_run_context_is_typed_immutable_and_has_zero_usage() -> None:
    context = RunContext(deps={'tenant': 'test'}, run_id=RUN_ID, conversation_id=CONVERSATION_ID)

    assert context.deps == {'tenant': 'test'}
    assert context.usage == Usage()
    with pytest.raises(FrozenInstanceError):
        context.usage = Usage.from_requests((make_request_usage(),))


def test_narrow_error_types_share_core_base() -> None:
    errors = (
        ConfigurationError(),
        CredentialError(),
        ProviderError(),
        ModelResolutionError(),
        AgentConstructionError(),
        ToolError(),
        PluginError(),
        TransportError(),
    )

    assert all(isinstance(error, OvidCoreError) for error in errors)


def test_all_normalized_events_serialize_through_discriminated_union() -> None:
    usage = Usage()
    events = (
        RunStartedEvent(run_id=RUN_ID, conversation_id=CONVERSATION_ID, sequence=0),
        ModelRequestStartedEvent(run_id=RUN_ID, conversation_id=CONVERSATION_ID, sequence=1, request_index=0),
        TextDeltaEvent(run_id=RUN_ID, conversation_id=CONVERSATION_ID, sequence=2, content='hi'),
        ToolCallEvent(
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
            sequence=3,
            tool_name='lookup',
            arguments={'key': 'value'},
            tool_call_id='call-1',
        ),
        ToolResultEvent(
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
            sequence=4,
            tool_name='lookup',
            content={'found': True},
            tool_call_id='call-1',
        ),
        UsageUpdateEvent(run_id=RUN_ID, conversation_id=CONVERSATION_ID, sequence=5, usage=usage),
        RunCompletedEvent(run_id=RUN_ID, conversation_id=CONVERSATION_ID, sequence=6, usage=usage),
        RunFailedEvent(
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
            sequence=7,
            error_type='provider_error',
            message='request failed',
        ),
    )
    adapter = TypeAdapter(AgentEvent)

    assert tuple(adapter.validate_json(adapter.dump_json(event)).kind for event in events) == tuple(
        event.kind for event in events
    )
