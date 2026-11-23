import sys
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import LocalWorkspaceBackend, WindowsWorkspaceBackend
from pydantic_ai_harness.skills import Skills

from ovid_core.errors import AgentConstructionError
from ovid_core.skills import SkillsCapability


class _SkillLibraries(Skills[Any]):
    async def for_run(self, ctx: RunContext[Any]) -> AbstractCapability[Any]:
        try:
            return await super().for_run(ctx)
        except Exception as error:
            raise AgentConstructionError('Agent Skills capability construction failed') from error


def adapt_skills_capability(source: SkillsCapability[Any]) -> AbstractCapability[Any]:
    config = source.config
    # A private host backend lets Skills read the libraries when the run has no workspace.
    host_backend = WindowsWorkspaceBackend if sys.platform == 'win32' else LocalWorkspaceBackend
    libraries = host_backend('.')

    if config.include is not None:
        skills = _SkillLibraries(
            config.directories,
            include=config.include,
            duplicate_names='keep_first',
            workspace=libraries,
        )
    else:
        skills = _SkillLibraries(
            config.directories,
            exclude=config.exclude,
            duplicate_names='keep_first',
            workspace=libraries,
        )
    skills.id = source.id

    return skills
