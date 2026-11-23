from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    LoadCapabilityCallPart,
    LoadCapabilityReturnPart,
    ModelMessage,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage
from pytest_mock import MockerFixture

import tests.support.integration_consumer as consumer
from ovid_core import AgentConstructionError, MessageCodec, SkillLibraryConfig, SkillsCapability
from ovid_core.adapters.pydantic_ai import adapt_capabilities, message_from_pydantic, message_to_pydantic
from ovid_core.messages import CapabilityLoadCallPart, CapabilityLoadReturnPart, OpaqueResponsePart
from tests.support.agent_helpers import agent_factory


def write_skill(directory: Path, name: str, description: str, body: str) -> None:
    skill = directory / name
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\n\n{body}\n',
        encoding='utf-8',
    )


async def run_leaves(adapted: AbstractCapability[None]) -> list[AbstractCapability[None]]:
    leaves: list[AbstractCapability[None]] = []
    context = RunContext[None](deps=None, model=TestModel(), usage=RunUsage())

    (await adapted.for_run(context)).apply(leaves.append)

    return leaves


async def test_skill_library_order_resolves_conflicts_before_selection(tmp_path: Path) -> None:
    personal = tmp_path / 'personal'
    project = tmp_path / 'project'
    write_skill(personal, 'code-review', 'Personal review.', 'Use personal policy.')
    write_skill(project, 'code-review', 'Project review.', 'Use project policy.')
    write_skill(project, 'release-notes', 'Write release notes.', 'Summarize changes.')
    source = SkillsCapability[None](
        id='layered-skills',
        config=SkillLibraryConfig(directories=(personal, project)),
    )
    adapted = adapt_capabilities((source,))[0]

    with pytest.warns(UserWarning, match="skill name 'code-review' is already taken"):
        leaves = await run_leaves(adapted)

    assert adapted.id == 'layered-skills'
    assert [leaf.id for leaf in leaves] == ['code-review', 'release-notes']
    assert leaves[0].get_instructions() == '# Skill: code-review\n\nUse personal policy.'


async def test_skill_libraries_select_validated_deferred_capabilities(tmp_path: Path) -> None:
    write_skill(tmp_path, 'code-review', 'Review code.', 'Inspect correctness.')
    write_skill(tmp_path, 'release-notes', 'Write release notes.', 'Summarize changes.')
    source = consumer.skills_capability(tmp_path)
    leaves = await run_leaves(adapt_capabilities((source,))[0])

    assert source.defer_loading is True
    assert [(leaf.id, leaf.description, leaf.defer_loading) for leaf in leaves] == [
        ('code-review', 'Review code.', True)
    ]
    assert leaves[0].get_instructions() == '# Skill: code-review\n\nInspect correctness.'

    with pytest.raises(ValidationError, match='cannot be combined'):
        SkillLibraryConfig(directories=(tmp_path,), include=(), exclude=())
    with pytest.raises(ValidationError):
        SkillLibraryConfig(directories=())
    unknown = SkillsCapability[None](
        id='unknown-skills',
        config=SkillLibraryConfig(directories=(tmp_path,), include=('missing',)),
    )
    with pytest.raises(AgentConstructionError, match='construction failed') as failure:
        await run_leaves(adapt_capabilities((unknown,))[0])

    assert isinstance(failure.value.__cause__, ValueError)
    assert 'Unknown skill in include: missing' in str(failure.value.__cause__)

    excluded = SkillsCapability[None](
        id='excluded-skills',
        config=SkillLibraryConfig(directories=(tmp_path,), exclude=('release-notes',)),
    )
    excluded_leaves = await run_leaves(adapt_capabilities((excluded,))[0])

    assert [leaf.id for leaf in excluded_leaves] == ['code-review']


async def test_relative_skill_library_resolves_from_build_working_directory(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    write_skill(tmp_path / 'skills', 'code-review', 'Review code.', 'Inspect correctness.')
    source = SkillsCapability[None](
        id='relative-skills',
        config=SkillLibraryConfig(directories=(Path('skills'),)),
    )
    working_directory = mocker.patch.object(Path, 'cwd', return_value=tmp_path)
    adapted = adapt_capabilities((source,))[0]
    working_directory.return_value = tmp_path / 'elsewhere'

    leaves = await run_leaves(adapted)

    assert [leaf.id for leaf in leaves] == ['code-review']


async def test_skill_loading_round_trips_and_continues_from_normalized_history(tmp_path: Path) -> None:
    write_skill(tmp_path, 'code-review', 'Review code.', 'Inspect correctness.')
    requests = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal requests
        requests += 1
        parts = [part for message in messages for part in message.parts]
        loaded = next((part for part in parts if isinstance(part, LoadCapabilityReturnPart)), None)

        if loaded is None:
            assert any(tool.name == 'load_capability' for tool in info.function_tools)
            assert info.instructions is not None
            assert '- code-review: Review code.' in info.instructions

            return ModelResponse(
                parts=(LoadCapabilityCallPart(args={'id': 'code-review'}, tool_call_id='load-skill'),),
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )

        assert 'code-review' in loaded.content.get('message', '')
        assert any(
            '# Skill: code-review\n\nInspect correctness.' in part.content
            for part in parts
            if isinstance(part, UserPromptPart)
        )

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
    load_returns = [part for message in history for part in message.parts if isinstance(part, CapabilityLoadReturnPart)]

    assert first.output == second.output == 'skill loaded'
    assert requests == 3
    assert any(isinstance(part, CapabilityLoadCallPart) for message in history for part in message.parts)
    assert [part.outcome for part in load_returns] == ['success']
    assert load_returns[0].message is not None


async def test_skill_adapter_rejects_invalid_library_and_keeps_invalid_capability_call_opaque(tmp_path: Path) -> None:
    source = SkillsCapability[None](
        id='missing-skills',
        config=SkillLibraryConfig(directories=(tmp_path / 'missing',)),
    )
    agent = await agent_factory({'primary': TestModel()}).build(consumer.integration_definition((source,)))
    with pytest.raises(AgentConstructionError, match='construction failed') as failure:
        await agent.run('Review this.', deps=None)

    assert isinstance(failure.value.__cause__, ValueError)
    assert 'Skill library directory does not exist' in str(failure.value.__cause__)

    upstream = ModelResponse(
        parts=(LoadCapabilityCallPart(args=None, tool_call_id='missing-id'),),
        usage=RequestUsage(),
    )
    normalized = message_from_pydantic(upstream)

    assert [type(part) for part in normalized.parts] == [OpaqueResponsePart]
    assert message_to_pydantic(normalized).parts == upstream.parts
