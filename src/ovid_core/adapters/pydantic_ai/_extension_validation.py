from collections.abc import Sequence

from ovid_core.capabilities.base import BaseCapability
from ovid_core.errors import ExtensionCollisionError
from ovid_core.tools.base import BaseToolset


def validate_extension_ids[Deps](
    capabilities: Sequence[BaseCapability[Deps]],
    direct_toolsets: Sequence[BaseToolset[Deps]] = (),
) -> None:
    capability_ids: set[str] = set()
    tool_ids: set[str] = set()
    toolset_ids: set[str] = set()

    for capability in capabilities:
        _add_unique(capability.id, capability_ids, 'capability')
        for tool in capability.contributions.tools:
            _add_unique(tool.id, tool_ids, 'tool')
        for toolset in capability.contributions.toolsets:
            _add_unique(toolset.id, toolset_ids, 'toolset')
            
    for toolset in direct_toolsets:
        _add_unique(toolset.id, toolset_ids, 'toolset')


def _add_unique(value: str, seen: set[str], kind: str) -> None:
    if not value:
        raise ExtensionCollisionError(f'{kind.capitalize()} IDs must not be empty')
    if value in seen:
        raise ExtensionCollisionError(f'Duplicate {kind} ID: {value!r}')

    seen.add(value)
