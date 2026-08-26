import httpx
from pydantic import Field, PositiveInt, ValidationError

from ovid_core.errors import ModelResolutionError
from ovid_core.models import BaseModel


class _CodexModelInfo(BaseModel):
    slug: str = Field(min_length=1)
    base_instructions: str = Field(min_length=1)
    context_window: PositiveInt | None = None


class CodexInstructionCatalog(BaseModel):
    models: tuple[_CodexModelInfo, ...]

    def instructions_for(self, model_name: str) -> str:
        return self._model_for(model_name).base_instructions

    def context_window_for(self, model_name: str) -> int | None:
        return self._model_for(model_name).context_window

    def model_names(self) -> tuple[str, ...]:
        return tuple(model.slug for model in self.models)

    def _model_for(self, model_name: str) -> _CodexModelInfo:
        for model in self.models:
            if model.slug == model_name:
                return model

        raise ModelResolutionError(f'Codex model catalog does not contain {model_name!r}')


async def load_instruction_catalog(
    *,
    http_client: httpx.AsyncClient,
    backend_url: str,
) -> CodexInstructionCatalog:
    endpoint = f'{backend_url.rstrip("/")}/models'
    try:
        response = await http_client.get(endpoint, params=httpx.QueryParams(client_version='0.0.0'))
        response.raise_for_status()
        return CodexInstructionCatalog.model_validate_json(response.content, extra='ignore')
    except httpx.HTTPError, ValidationError:
        raise ModelResolutionError('Codex model catalog request failed') from None
