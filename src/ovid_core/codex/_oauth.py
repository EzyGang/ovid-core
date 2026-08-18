import httpx
from pydantic import ValidationError

from ovid_core.codex.models import CodexOAuthConfig, CodexTokens, _AuthorizationCodeExchangeRequest, _OAuthTokenResponse
from ovid_core.errors import CodexAuthError


_FORM_HEADERS = httpx.Headers({'Content-Type': 'application/x-www-form-urlencoded'})


async def exchange_authorization_code(
    *,
    http_client: httpx.AsyncClient,
    config: CodexOAuthConfig,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> CodexTokens:
    try:
        request = _AuthorizationCodeExchangeRequest(
            code=code,
            redirect_uri=redirect_uri,
            client_id=config.client_id,
            code_verifier=code_verifier,
        )
        response = await http_client.post(
            f'{config.issuer.rstrip("/")}/oauth/token',
            data=request.model_dump(mode='json'),
            headers=_FORM_HEADERS,
        )
        if not response.is_success:
            raise CodexAuthError(f'Codex token exchange failed with status {response.status_code}')
        payload = _OAuthTokenResponse.model_validate_json(response.content, extra='ignore')
    except httpx.HTTPError, ValidationError:
        raise CodexAuthError('Codex token exchange failed') from None

    return CodexTokens(
        id_token=payload.id_token,
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
    )
