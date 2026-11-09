from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import database
import main
from zaptec_api import ZaptecAuthError

INSTALLATIONS = [
    {"Id": "inst-a", "Name": "Garasje A"},
    {"Id": "inst-b", "Name": "Garasje B"},
]
SESSIONS = {
    "inst-a": [{"Energy": 10.0, "StartDateTime": "2026-10-02T10:00:00", "DeviceName": "L1"}],
    "inst-b": [],
}


class FakeZaptec:
    def __init__(self):
        self.expired = False

    async def login(self, username, password):
        if password != "riktig":
            raise ZaptecAuthError("bad")
        return {"access_token": "zaptec-token", "expires_in": 3600}

    async def get_installations(self, token):
        if self.expired:
            raise ZaptecAuthError("expired")
        assert token == "zaptec-token"
        return INSTALLATIONS

    async def get_chargehistory(self, token, installation_id, from_date, to_date):
        return SESSIONS[installation_id]


@pytest.fixture
def make_client(tmp_path, monkeypatch, fake_fiken):
    def make(**overrides):
        values = {
            "database_path": str(tmp_path / "app.db"),
            "allowed_users": frozenset({"endre@example.com"}),
            "fiken_api_token": "test-token",
            "fiken_company_slug": "test-as",
            **overrides,
        }
        settings = replace(main.settings, **values)
        monkeypatch.setattr(main, "settings", settings)
        monkeypatch.setattr(main, "zaptec_api", FakeZaptec())
        monkeypatch.setattr(
            main, "fiken_api", fake_fiken.client() if settings.fiken_enabled else None
        )
        return TestClient(main.app, base_url="https://testserver")

    return make


def login(client, username="endre@example.com", password="riktig"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def test_requires_login(make_client):
    with make_client() as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_login_sets_secure_cookie_and_shows_overview(make_client):
    with make_client() as client:
        response = login(client)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        assert "max-age=3600" in cookie  # capped by Zaptec token lifetime

        page = client.get("/?year=2026&month=10")
        assert page.status_code == 200
        assert "Garasje A" in page.text and "10,000 kWh" in page.text
        assert "Velg kunde" in page.text


def test_user_not_on_allowlist_is_refused(make_client):
    with make_client() as client:
        response = login(client, username="someone@example.com")
        assert response.status_code == 401
        assert "ikke tilgang" in response.text


def test_fiken_without_allowlist_refuses_everyone(make_client):
    with make_client(allowed_users=frozenset()) as client:
        response = login(client)
        assert response.status_code == 401
        assert "ALLOWED_USERS" in response.text


def test_wrong_password(make_client):
    with make_client() as client:
        assert login(client, password="feil").status_code == 401


def test_expired_zaptec_token_logs_out(make_client):
    with make_client() as client:
        login(client)
        main.zaptec_api.expired = True
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_map_customer_and_create_drafts(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        picker = client.get("/installations/inst-a/customer?year=2026&month=10&q=ola")
        assert "Ola Nordmann" in picker.text and "Kari Hansen" not in picker.text

        response = client.post(
            "/installations/inst-a/customer",
            data={"contact_id": 11, "year": 2026, "month": 10},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_customer_mappings() == {"inst-a": 11}

        response = client.post(
            "/drafts",
            data={"year": 2026, "month": 10, "nok_per_kwh": "1.50"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/?year=2026&month=10&nok_per_kwh=1.50"

        drafts = database.get_drafts(2026, 10)
        assert drafts["inst-a"].status == "created"
        assert drafts["inst-b"].status == "skipped"
        assert len(fake_fiken.drafts) == 1

        page = client.get(response.headers["location"])
        assert "Utkast opprettet" in page.text and "Ola Nordmann" in page.text


def test_unknown_customer_id_is_rejected(make_client):
    with make_client() as client:
        login(client)
        response = client.post(
            "/installations/inst-a/customer",
            data={"contact_id": 999, "year": 2026, "month": 10},
        )
        assert response.status_code == 404
        assert database.get_customer_mappings() == {}


def test_installation_must_belong_to_user(make_client):
    with make_client() as client:
        login(client)
        response = client.post(
            "/installations/someone-else/customer",
            data={"contact_id": 11, "year": 2026, "month": 10},
        )
        assert response.status_code == 404


def test_invalid_price_shows_error_page(make_client):
    with make_client() as client:
        login(client)
        response = client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "-2"})
        assert response.status_code == 400
        assert "Ugyldig pris" in response.text


def test_export_all_zip(make_client):
    with make_client(fiken_api_token="") as client:
        client.post("/login", data={"username": "endre@example.com", "password": "riktig"})
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


def test_logout_requires_post_and_clears_session(make_client):
    with make_client() as client:
        login(client)
        assert client.get("/logout").status_code in (404, 405)
        response = client.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_mapping_to_unknown_fiken_customer_is_not_drafted(make_client, fake_fiken):
    with make_client() as client:
        login(client)
        database.set_customer_mapping("inst-a", 999)  # deleted in Fiken
        page = client.get("/?year=2026&month=10")
        assert "Fant ikke kunden" in page.text
        assert "Opprett 0 utkast" in page.text

        client.post("/drafts", data={"year": 2026, "month": 10, "nok_per_kwh": "1.5"})
        assert database.get_drafts(2026, 10)["inst-a"].status == "missing_customer"
        assert fake_fiken.drafts == []
