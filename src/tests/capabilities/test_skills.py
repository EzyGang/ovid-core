from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import LoadCapabilityCallPart, LoadCapabilityReturnPart, ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

import tests.support.integration_consumer as consumer
from ovid_core import AgentConstructionError, MessageCodec, ProviderError, SkillLibraryConfig, SkillsCapability
from ovid_core.adapters.pydantic_ai import adapt_capabilities, message_from_pydantic
from ovid_core.messages import CapabilityLoadCallPart, CapabilityLoadReturnPart
from tests.support.agent_helpers import agent_factory


def write_skill(directory: Path, name: str, description: str, body: str) -> None:
    skill = directory / name
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\n\n{body}\n',
        encoding='utf-8',
    )


def test_skill_libraries_select_validated_deferred_capabilities(tmp_path: Path) -> None:
    write_skill(tmp_path, 'code-review', 'Review code.', 'Inspect correctness.')
    write_skill(tmp_path, 'release-notes', 'Write release notes.', 'Summarize changes.')
    source = consumer.skills_capability(tmp_path)
    adapted = adapt_capabilities((source,))[0]
    leaves: list[Capability[None]] = []
    adapted.apply(leaves.append)

    assert source.defer_loading is True
    assert [(leaf.id, leaf.description, leaf.defer_loading) for leaf in leaves] == [
        ('code-review', 'Review code.', True)
    ]
    assert leaves[0].get_instructions() == ['# Skill: code-review\n\nInspect correctness.']

    with pytest.raises(ValidationError, match='cannot be combined'):
        SkillLibraryConfig(directories=(tmp_path,), include=(), exclude=())
    with pytest.raises(ValidationError):
        SkillLibraryConfig(directories=())

    excluded = SkillsCapability[None](
        id='excluded-skills',
        config=SkillLibraryConfig(directories=(tmp_path,), exclude=('release-notes',)),
    )
    excluded_leaves: list[Capability[None]] = []
    adapt_capabilities((excluded,))[0].apply(excluded_leaves.append)

    assert [leaf.id for leaf in excluded_leaves] == ['code-review']


async def test_skill_loading_round_trips_and_continues_from_normalized_history(tmp_path: Path) -> None:
    write_skill(tmp_path, 'code-review', 'Review code.', 'Inspect correctness.')
    requests = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal requests
        requests += 1
        loaded = next(
            (part for message in messages for part in message.parts if isinstance(part, LoadCapabilityReturnPart)),
            None,
        )

        if loaded is None:
            assert any(tool.name == 'load_capability' for tool in info.function_tools)

            return ModelResponse(
                parts=(LoadCapabilityCallPart(args={'id': 'code-review'}, tool_call_id='load-skill'),),
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )

        assert loaded.instructions is not None
        assert 'Inspect correctness.' in loaded.instructions

        return ModelResponse(
            parts=(TextPart('skill loaded'),),
            usage=RequestUsage(input_tokens=len(messages), output_tokens=1),
        )

    model = FunctionModel(respond, model_name='skills')
    definition = consumer.integration_definition((consumer.skills_capability(tmp_path),))
    agent = await agent_factory({'primary': model}).build(definition)
    first = await agent.run('Review this.', deps=None)
    codec = MessageCodec()
    history = tuple(codec.decode(codec.encode(message)) for message in first.messages)
    second = await agent.run('Continue.', deps=None, messages=history)

    assert first.output == second.output == 'skill loaded'
    assert requests == 3
    assert any(isinstance(part, CapabilityLoadCallPart) for message in history for part in message.parts)
    assert any(isinstance(part, CapabilityLoadReturnPart) for message in history for part in message.parts)
    assert codec.version == 2


def test_skill_adapter_safely_rejects_invalid_library_and_capability_call(tmp_path: Path) -> None:
    source = SkillsCapability[None](
        id='missing-skills',
        config=SkillLibraryConfig(directories=(tmp_path / 'missing',)),
    )
    with pytest.raises(AgentConstructionError, match='construction failed'):
        adapt_capabilities((source,))

    upstream = ModelResponse(
        parts=(LoadCapabilityCallPart(args=None, tool_call_id='missing-id'),),
        usage=RequestUsage(),
    )
    with pytest.raises(ProviderError, match='unsupported or invalid message'):
        message_from_pydantic(upstream)
