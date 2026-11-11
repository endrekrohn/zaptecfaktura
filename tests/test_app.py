import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import database
import main
from conftest import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
from usage import UsageService
from zaptec_api import ChargeSession, UsageTotal, ZaptecAuthError

USER = "endre@example.com"
INSTALLATIONS = [
    {"Id": "inst-a", "Name": "Garasje A"},
    {"Id": "inst-b", "Name": "Garasje B"},
]
SESSIONS = {
    "inst-a": [ChargeSession(None, None, "L1", 10.0, False)],
    "inst-b": [],
}


class FakeZaptec:
    def __init__(self):
        self.expired = False
        self.session_calls = 0

    async def login(self, username, password):
        if password != "riktig":
            raise ZaptecAuthError("bad")
        return {"access_token": "zaptec-token", "expires_in": 3600}

    async def get_installations(self, token):
        if self.expired:
            raise ZaptecAuthError("expired")
        assert token == "zaptec-token"
        return list(INSTALLATIONS)

    async def get_usage_total(self, token, installation_id, start, end):
        if self.expired:
            raise ZaptecAuthError("expired")
        sessions = SESSIONS[installation_id]
        return UsageTotal(sum(s.energy for s in sessions), len(sessions))

    async def get_archived_sessions(self, token, installation_id, start, end):
        self.session_calls += 1
        return SESSIONS[installation_id]

    async def aclose(self):
        pass


@pytest.fixture
def make_client(tmp_path, monkeypatch, fake_fiken):
    def make(**overrides):
        values = {
            "database_path": str(tmp_path / "app.db"),
            "allowed_users": frozenset(),
            "secret_key": "test-secret",
            "fiken_client_id": CLIENT_ID,
            "fiken_client_secret": CLIENT_SECRET,
            "fiken_redirect_uri": REDIRECT_URI,
            **overrides,
        }
        monkeypatch.setattr(main, "settings", replace(main.settings, **values))
        monkeypatch.setattr(main, "usage_service", UsageService(FakeZaptec()))
        monkeypatch.setattr(main, "fiken_transport", fake_fiken.transport)
        monkeypatch.setattr(main, "_fiken_tokens", None)
        monkeypatch.setattr(main, "_fiken_http", None)
        monkeypatch.setattr(main, "_customers_cache", {})
        return TestClient(main.app, base_url="https://testserver")

    return make


def login(client, username=USER, password="riktig"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def connect_fiken(client, fake_fiken):
    """Run the OAuth flow: our redirect to Fiken, consent, and the callback."""
    response = client.post("/fiken/connect", follow_redirects=False)
    assert response.status_code == 303
    url = urlparse(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://fiken.no/oauth/authorize"
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URI
    assert params["code_challenge_method"] == "S256"
    code = fake_fiken.issue_code(params["code_challenge"])
    return client.get(
        f"/fiken/callback?code={code}&state={params['state']}", follow_redirects=False
    )


# --- login ---------------------------------------------------------------


def test_requires_login(make_client):
    with make_client() as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_login_sets_secure_cookie(make_client):
    with make_client() as client:
        response = login(client)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        assert "max-age=3600" in cookie  # capped by Zaptec token lifetime


def test_optional_allowlist(make_client):
    with make_client(allowed_users=frozenset({USER})) as client:
        response = login(client, username="someone@example.com")
        assert response.status_code == 401
        assert "ikke tilgang" in response.text
        assert login(client).status_code == 303


def test_wrong_password(make_client):
    with make_client() as client:
        assert login(client, password="feil").status_code == 401


def test_expired_zaptec_token_logs_out(make_client):
    with make_client() as client:
        login(client)
        main.usage_service.zaptec.expired = True
        main.usage_service.forget_user(USER)
        response = client.get("/", follow_redirects=False)
        assert response.headers["location"] == "/login"


def test_logout_requires_post_and_clears_session(make_client):
    with make_client() as client:
        login(client)
        assert client.get("/logout").status_code in (404, 405)
        client.post("/logout", follow_redirects=False)
        assert client.get("/", follow_redirects=False).status_code == 303


# --- Fiken connection ----------------------------------------------------


def test_overview_prompts_to_connect_fiken(make_client):
    with make_client() as client:
        login(client)
        page = client.get("/?year=2026&month=10")
        assert "Garasje A" in page.text and "10,000 kWh" in page.text
        assert "Koble til Fiken" in page.text
        assert "id=\"create-drafts\"" not in page.text  # no draft button until connected


def test_connect_fiken_with_single_company(make_client, fake_fiken, tmp_path):
    with make_client() as client:
        login(client)
        response = connect_fiken(client, fake_fiken)
        assert response.status_code == 303
        # First connection goes to the invoice settings.
        assert response.headers["location"] == "/fiken/innstillinger?welcome=1"

        connection = database.get_fiken_connection(USER)
        assert connection.company_slug == "test-as"
        assert connection.company_name == "Test AS"

        # Tokens are encrypted at rest.
        raw = sqlite3.connect(tmp_path / "app.db").execute(
            "SELECT access_token, refresh_token FROM fiken_connection"
        ).fetchone()
        assert "access-1" not in raw[0] and "refresh-1" not in raw[1]

        page = client.get("/?year=2026&month=10")
        assert "Velg" in page.text
        assert "Test AS" in client.get("/fiken").text


def test_connect_with_several_companies_shows_picker(make_client, fake_fiken):
    fake_fiken.companies.append(
        {"slug": "annet-as", "name": "Annet AS", "hasApiAccess": False}
    )
    with make_client() as client:
        login(client)
        response = connect_fiken(client, fake_fiken)
        assert response.headers["location"] == "/fiken/company"
        assert database.get_fiken_connection(USER).company_slug is None

        picker = client.get("/fiken/company")
        assert "Test AS" in picker.text and "Mangler API-modul" in picker.text

        response = client.post("/fiken/company", data={"slug": "test-as"}, follow_redirects=False)
        assert response.status_code == 303
        assert database.get_fiken_connection(USER).company_slug == "test-as"

        assert client.post("/fiken/company", data={"slug": "ukjent"}).status_code == 404


def test_callback_rejects_wrong_state(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        client.post("/fiken/connect", follow_redirects=False)
        response = client.get("/fiken/callback?code=x&state=forged", follow_redirects=False)
        assert response.headers["location"] == "/fiken?error=state"
        assert database.get_fiken_connection(USER) is None


def test_callback_without_cookie_is_rejected(make_client):
    with make_client() as client:
        login(client)
        response = client.get("/fiken/callback?code=x&state=y", follow_redirects=False)
        assert response.headers["location"] == "/fiken?error=state"


def test_user_denies_consent(make_client):
    with make_client() as client:
        login(client)
        client.post("/fiken/connect", follow_redirects=False)
        response = client.get("/fiken/callback?error=access_denied", follow_redirects=False)
        assert response.headers["location"] == "/fiken?error=denied"
        assert "avbrøt" in client.get("/fiken?error=denied").text


def test_disconnect_revokes_at_fiken(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        client.post("/fiken/disconnect")
        assert fake_fiken.revoked == ["access-1"]
        assert database.get_fiken_connection(USER) is None


def test_revoked_consent_sends_user_to_reconnect(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        fake_fiken.valid_access.clear()
        fake_fiken.valid_refresh.clear()  # user removed the app in Fiken

        page = client.get("/?year=2026&month=10")
        assert page.status_code == 200
        assert "Koble til Fiken" in page.text
        assert database.get_fiken_connection(USER) is None

        response = client.get(
            "/installations/inst-a/customer?year=2026&month=10", follow_redirects=False
        )
        assert response.headers["location"] == "/fiken"


def test_fiken_pages_need_server_config(make_client):
    with make_client(fiken_client_secret="") as client:
        login(client)
        assert client.get("/fiken").status_code == 503
        page = client.get("/?year=2026&month=10")
        assert "ikke satt opp på serveren" in page.text


# --- customers and drafts ------------------------------------------------


def test_map_customer_and_create_drafts(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)

        picker = client.get("/installations/inst-a/customer?year=2026&month=10&q=ola")
        assert "Ola Nordmann" in picker.text and "Kari Hansen" not in picker.text

        response = client.post(
            "/installations/inst-a/customer",
            data={"contact_id": 11, "year": 2026, "month": 10},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_customer_mappings("test-as") == {"inst-a": 11}

        response = client.post(
            "/drafts",
            data={"year": 2026, "month": 10, "nok_per_kwh": "1.50"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/?year=2026&month=10&nok_per_kwh=1.50"

        drafts = database.get_drafts("test-as", 2026, 10)
        assert drafts["inst-a"].status == "created"
        assert drafts["inst-b"].status == "skipped"
        [draft] = fake_fiken.drafts
        assert draft["customerId"] == 11
        assert len(draft["attachments"]) == 1

        page = client.get(response.headers["location"])
        assert f">#{drafts['inst-a'].fiken_draft_id}</a>" in page.text and "Utkast opprettet" not in page.text
        assert "Ola Nordmann" in page.text and "Ingen forbruk" not in page.text


def test_drafts_require_connection(make_client):
    with make_client() as client:
        login(client)
        response = client.post(
            "/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"}, follow_redirects=False
        )
        assert response.headers["location"] == "/fiken"


def test_mapping_to_unknown_fiken_customer_is_not_drafted(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        database.set_customer_mapping("test-as", "inst-a", 999)  # deleted in Fiken
        page = client.get("/?year=2026&month=10")
        assert "Fant ikke kunden" in page.text
        assert "Opprett 0 utkast" in page.text

        client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})
        assert database.get_drafts("test-as", 2026, 10)["inst-a"].status == "missing_customer"
        assert fake_fiken.drafts == []


def test_unknown_customer_id_is_rejected(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        response = client.post(
            "/installations/inst-a/customer", data={"contact_id": 999, "year": 2026, "month": 10}
        )
        assert response.status_code == 404
        assert database.get_customer_mappings("test-as") == {}


def test_installation_must_belong_to_user(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        response = client.post(
            "/installations/someone-else/customer",
            data={"contact_id": 11, "year": 2026, "month": 10},
        )
        assert response.status_code == 404


def test_invalid_price_shows_error_page(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        response = client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "-2"})
        assert response.status_code == 400
        assert "Ugyldig pris" in response.text


# --- export works without Fiken -----------------------------------------


def test_export_all_zip(make_client):
    with make_client(fiken_client_id="") as client:
        login(client)
        response = client.post("/exportall", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"


def test_export_single_pdf(make_client):
    with make_client() as client:
        login(client)
        response = client.post(
            "/export",
            data={"installation_id": "inst-a", "year": 2026, "month": 10, "nok_per_kwh": "1.5"},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")
        assert "2026_10_grunnlag_Garasje_A.pdf" in response.headers["content-disposition"]


def test_drafts_status_endpoint(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        assert client.get("/drafts/status?year=2026&month=10").json() == {"pending": False, "drafts": {}}
        connect_fiken(client, fake_fiken)
        client.post("/installations/inst-a/customer", data={"contact_id": 11, "year": 2026, "month": 10})
        client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})
        data = client.get("/drafts/status?year=2026&month=10").json()
        assert data["pending"] is False
        assert data["drafts"]["inst-a"]["label"] == "Utkast opprettet"
        assert data["drafts"]["inst-a"]["kwh"] == 10.0
        assert data["drafts"]["inst-b"]["status"] == "skipped"
        assert data["drafts"]["inst-b"]["url"] is None


def test_link_to_draft_in_fiken(make_client, fake_fiken):
    from billing import draft_uuid

    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        client.post("/installations/inst-a/customer", data={"contact_id": 11, "year": 2026, "month": 10})
        client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})

        url = f"https://fiken.no/foretak/test-as/fakturautkast/{draft_uuid('inst-a', 2026, 10)}"
        assert fake_fiken.drafts[0]["uuid"] == draft_uuid("inst-a", 2026, 10)
        assert client.get("/drafts/status?year=2026&month=10").json()["drafts"]["inst-a"]["url"] == url
        page = client.get("/?year=2026&month=10")
        draft_id = fake_fiken.drafts[0]["draftId"]
        assert f'href="{url}" target="_blank" rel="noopener" title="Åpne utkastet i Fiken">#{draft_id}</a>' in page.text
        assert "Åpne utkastet i Fiken ↗" not in page.text


def test_overview_does_not_fetch_sessions(make_client):
    with make_client() as client:
        login(client)
        client.get("/?year=2026&month=10")
        client.get("/?year=2026&month=10&refresh=1")
        assert main.usage_service.zaptec.session_calls == 0


def test_pending_page_polls_instead_of_reloading(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        database.upsert_draft(
            database.DraftRecord("test-as", "inst-a", 2026, 10, "u", 11, 10.0, 150, "pending")
        )
        page = client.get("/?year=2026&month=10")
        assert 'http-equiv="refresh"' not in page.text
        assert "const pending = true" in page.text


# --- invoice settings ----------------------------------------------------


def test_invoice_settings_need_a_connection(make_client):
    with make_client() as client:
        login(client)
        response = client.get("/fiken/innstillinger", follow_redirects=False)
        assert response.headers["location"] == "/fiken"


def test_fiken_menu_shows_settings_once_connected(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        page = client.get("/?year=2026&month=10")
        assert 'data-menu-trigger' in page.text and "Logg ut" in page.text
        assert 'href="/fiken/innstillinger"' not in page.text
        connect_fiken(client, fake_fiken)
        page = client.get("/?year=2026&month=10")
        assert 'href="/fiken/innstillinger"' in page.text


def test_save_invoice_settings_and_use_them(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)

        page = client.get("/fiken/innstillinger?welcome=1")
        assert "Fiken er tilkoblet" in page.text
        assert "3000 Salgsinntekt, avgiftspliktig" in page.text
        assert 'value="3600"' in page.text and "Høy sats (25 %)" in page.text

        response = client.post(
            "/fiken/innstillinger",
            data={"income_account": "3600", "vat_type": "EXEMPT", "days_until_due": 30},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/fiken/innstillinger?saved=1"
        assert tuple(database.get_invoice_settings("test-as")) == ("3600", "EXEMPT", 30)
        page = client.get("/fiken/innstillinger?saved=1")
        assert '<option value="3600" selected>' in page.text
        assert '<option value="EXEMPT" selected>' in page.text

        client.post("/installations/inst-a/customer", data={"contact_id": 11, "year": 2026, "month": 10})
        client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})
        draft = fake_fiken.drafts[0]
        assert draft["daysUntilDueDate"] == 30
        assert draft["lines"][0]["incomeAccount"] == "3600"
        assert draft["lines"][0]["vatType"] == "EXEMPT"

        # Reconnecting later goes straight to the overview.
        client.post("/fiken/disconnect")
        assert connect_fiken(client, fake_fiken).headers["location"] == "/"


@pytest.mark.parametrize(
    "data,message",
    [
        ({"income_account": "1920", "vat_type": "HIGH", "days_until_due": 14}, "inntektskonto fra listen"),
        ({"income_account": "3000", "vat_type": "HIGH_DIRECT", "days_until_due": 14}, "gyldig mva-type"),
        ({"income_account": "3000", "vat_type": "HIGH", "days_until_due": 400}, "mellom 0 og 365"),
    ],
)
def test_invalid_invoice_settings_are_rejected(make_client, fake_fiken, data, message):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        response = client.post("/fiken/innstillinger", data=data)
        assert response.status_code == 400
        assert message in response.text
        assert database.get_invoice_settings("test-as") is None


def test_defaults_are_used_until_settings_are_saved(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        connect_fiken(client, fake_fiken)
        page = client.get("/fiken/innstillinger")
        assert '<option value="3000" selected>' in page.text
