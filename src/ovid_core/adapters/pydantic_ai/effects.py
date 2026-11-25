from pydantic import ValidationError
from pydantic_ai.tools import ToolDefinition

from ovid_core.tools.models import ToolEffect


def resolve_effect(tool_def: ToolDefinition) -> ToolEffect:
    """Read the effect of any tool. Invalid or partial metadata gives the conservative default."""
    metadata = tool_def.metadata or {}

    if 'ovid_effect' in metadata:
        declared = metadata['ovid_effect']
        if not isinstance(declared, dict) or declared.keys() != {'kind', 'replay'}:
            return ToolEffect()
        try:
            return ToolEffect.model_validate(declared)
        except ValidationError:
            return ToolEffect()

    annotations = metadata.get('annotations')
    if not isinstance(annotations, dict) or annotations.get('readOnlyHint') is not True:
        return ToolEffect()

    idempotent = annotations.get('idempotentHint')
    if idempotent is not None and not isinstance(idempotent, bool):
        return ToolEffect()

    return ToolEffect(kind='read', replay='safe' if idempotent else 'unsafe')
