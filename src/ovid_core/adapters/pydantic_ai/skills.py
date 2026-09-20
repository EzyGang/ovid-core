from typing import Any, cast

from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai_harness.skills import Skills

from ovid_core.errors import AgentConstructionError
from ovid_core.skills import SkillsCapability


def adapt_skills_capability(source: SkillsCapability[Any]) -> AbstractCapability[Any]:
    try:
        leaves: list[AbstractCapability[Any]] = []
        seen: set[str] = set()
        for directory in source.config.directories:
            discovered: list[AbstractCapability[Any]] = []
            Skills(directory).apply(discovered.append)
            for capability in discovered:
                capability_id = cast(str, capability.id)
                if capability_id not in seen:
                    leaves.append(capability)
                    seen.add(capability_id)

        selected = _selected_names(source, seen)
        return CombinedCapability(
            [capability for capability in leaves if capability.id in selected],
            id=source.id,
        )
    except Exception:
        raise AgentConstructionError('Agent Skills capability construction failed') from None


def _selected_names(source: SkillsCapability[Any], available: set[str]) -> set[str]:
    include = source.config.include
    exclude = source.config.exclude
    requested = frozenset(include if include is not None else (exclude or ()))
    unknown = requested.difference(available)
    if unknown:
        names = ', '.join(sorted(unknown))
        raise ValueError(f'Unknown skills: {names}')
    if include is not None:
        return set(include)
    return available.difference(exclude or ())
