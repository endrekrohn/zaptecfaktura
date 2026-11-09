import asyncio
import time

import pytest

import database
from conftest import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
from fiken_api import FikenAPI
from fiken_oauth import FikenNotConnected, FikenOAuth, FikenTokens, TokenSet, new_pkce_pair

USER = "Endre@Example.com"


@pytest.fixture
def tokens(fake_fiken):
    oauth = FikenOAuth(
        "https://fiken.no/oauth", CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, transport=fake_fiken.transport
    )
    return FikenTokens(oauth, "secret")


def test_pkce_pair():
    verifier, challenge = new_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge and len(challenge) == 43


def test_valid_token_is_used_without_refresh(tokens, fake_fiken):
    tokens.save(USER, TokenSet("access-0", "refresh-0", time.time() + 3600))
    assert asyncio.run(tokens.access_token(USER)) == "access-0"
    assert fake_fiken.refresh_calls == 0


def test_expiring_token_is_refreshed_and_stored(tokens, fake_fiken):
    tokens.save(USER, TokenSet("access-0", "refresh-0", time.time() + 30))
    assert asyncio.run(tokens.access_token(USER)) == "access-1"
    assert fake_fiken.refresh_calls == 1
    # The new refresh token replaced the old one.
    assert asyncio.run(tokens.access_token(USER.lower(), force_refresh=True)) == "access-2"


def test_invalid_refresh_token_disconnects(tokens, fake_fiken):
    tokens.save(USER, TokenSet("access-0", "stale", time.time() - 1))
    with pytest.raises(FikenNotConnected):
        asyncio.run(tokens.access_token(USER))
    assert database.get_fiken_connection(USER) is None


def test_changed_secret_key_means_reconnect(tokens, fake_fiken):
    tokens.save(USER, TokenSet("access-0", "refresh-0", time.time() + 3600))
    other = FikenTokens(tokens.oauth, "another-secret")
    with pytest.raises(FikenNotConnected):
        asyncio.run(other.access_token(USER))


def test_api_retries_once_with_fresh_token_on_401(tokens, fake_fiken):
    # Stored token looks valid locally but Fiken has revoked it.
    tokens.save(USER, TokenSet("revoked", "refresh-0", time.time() + 3600))
    client = FikenAPI(
        "https://api.fiken.no/api/v2",
        lambda force: tokens.access_token(USER, force),
        "test-as",
        transport=fake_fiken.transport,
    )
    customers = asyncio.run(client.list_customers())
    assert [c["contactId"] for c in customers] == [11, 22]
    assert fake_fiken.refresh_calls == 1
