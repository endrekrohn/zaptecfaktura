"""Fiken OAuth2 (authorization code flow with PKCE) and per-user token storage.

The app is a confidential client: it authenticates at the token endpoint with
its client id and secret (HTTP Basic). PKCE is used as well, as Fiken
recommends, so an intercepted authorization code is useless on its own.

Each Zaptec user connects their own Fiken account once. Access and refresh
tokens are stored encrypted (Fernet, key derived from SECRET_KEY) and the
access token is refreshed shortly before it expires.
"""

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

import database

REFRESH_MARGIN_SECONDS = 120


class FikenOAuthError(Exception):
    def __init__(self, message: str, error: str | None = None):
        super().__init__(message)
        self.error = error


class FikenNotConnected(Exception):
    """The user has no (working) Fiken connection and must connect again."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float


def new_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE with S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class FikenOAuth:
    def __init__(
        self,
        oauth_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.oauth_url = oauth_url.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._transport = transport

    def authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.oauth_url}/authorize?{urlencode(params)}"

    async def _token_request(self, data: dict[str, str], previous_refresh: str = "") -> TokenSet:
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as client:
            response = await client.post(
                f"{self.oauth_url}/token",
                data=data,
                auth=(self.client_id, self._client_secret),
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            error = body.get("error")
            raise FikenOAuthError(
                f"Fiken token request failed ({response.status_code}): "
                f"{body.get('error_description') or error or response.text[:200]}",
                error,
            )
        return TokenSet(
            access_token=body["access_token"],
            # The refresh response contains "a possibly updated refresh token".
            refresh_token=body.get("refresh_token") or previous_refresh,
            expires_at=time.time() + float(body.get("expires_in", 3600)),
        )

    async def exchange_code(self, code: str, code_verifier: str, state: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            previous_refresh=refresh_token,
        )

    async def revoke(self, access_token: str) -> None:
        """Revoke the grant, the access token and its refresh token."""
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as client:
            response = await client.post(
                f"{self.oauth_url}/revoke",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            raise FikenOAuthError(f"Fiken revoke failed ({response.status_code})")


class FikenTokens:
    """Encrypted per-user token storage with refresh on demand."""

    def __init__(self, oauth: FikenOAuth, secret_key: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
        self._fernet = Fernet(key)
        self.oauth = oauth
        self._locks: dict[str, asyncio.Lock] = {}

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            # SECRET_KEY changed: the stored tokens can't be used any more.
            raise FikenNotConnected("Stored Fiken tokens can't be decrypted") from None

    def save(self, zaptec_user: str, tokens: TokenSet) -> None:
        database.save_fiken_tokens(
            zaptec_user,
            self._encrypt(tokens.access_token),
            self._encrypt(tokens.refresh_token),
            tokens.expires_at,
        )

    async def access_token(self, zaptec_user: str, force_refresh: bool = False) -> str:
        lock = self._locks.setdefault(zaptec_user.lower(), asyncio.Lock())
        async with lock:
            connection = database.get_fiken_connection(zaptec_user)
            if connection is None:
                raise FikenNotConnected("Fiken is not connected")
            if not force_refresh and connection.expires_at - REFRESH_MARGIN_SECONDS > time.time():
                return self._decrypt(connection.access_token)
            try:
                tokens = await self.oauth.refresh(self._decrypt(connection.refresh_token))
            except FikenOAuthError as e:
                if e.error in ("invalid_grant", "unauthorized_client"):
                    # Consent revoked or refresh token expired: connect again.
                    database.delete_fiken_connection(zaptec_user)
                    raise FikenNotConnected(str(e)) from e
                raise
            self.save(zaptec_user, tokens)
            return tokens.access_token

    async def disconnect(self, zaptec_user: str) -> None:
        """Revoke at Fiken (best effort) and forget the tokens."""
        connection = database.get_fiken_connection(zaptec_user)
        if connection is None:
            return
        try:
            await self.oauth.revoke(self._decrypt(connection.access_token))
        except (FikenOAuthError, FikenNotConnected, httpx.HTTPError):
            pass
        database.delete_fiken_connection(zaptec_user)
