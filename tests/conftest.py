import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

import database
import drafts
import fiken_api


@pytest.fixture(autouse=True)
def db(tmp_path):
    database.init_db(str(tmp_path / "test.db"))
    yield


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    # Module-level locks bind to the event loop that first waits on them;
    # each test runs its own loop.
    monkeypatch.setattr(fiken_api, "MIN_INTERVAL", 0)
    monkeypatch.setattr(fiken_api.FikenAPI, "_lock", asyncio.Lock())
    monkeypatch.setattr(drafts, "_job_lock", asyncio.Lock())


CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"
REDIRECT_URI = "https://testserver/fiken/callback"


class FakeFiken:
    """In-memory stand-in for Fiken's OAuth server and the API endpoints used."""

    def __init__(self):
        self.companies = [
            {"slug": "test-as", "name": "Test AS", "organizationNumber": "912345678", "hasApiAccess": True}
        ]
        self.customers = [
            {"contactId": 11, "name": "Ola Nordmann", "customerNumber": 10001, "customer": True},
            {"contactId": 22, "name": "Kari Hansen AS", "organizationNumber": "999888777", "customer": True},
        ]
        self.drafts: list[dict] = []
        self.invoices: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.fail_attachment = False
        self.fail_create_for: set[int] = set()
        self.next_id = 1000
        # OAuth state
        self.valid_access = {"access-0"}
        self.valid_refresh = {"refresh-0"}
        self.codes: dict[str, str] = {}  # code -> code_challenge
        self.token_counter = 0
        self.revoked: list[str] = []
        self.refresh_calls = 0

    # --- OAuth ------------------------------------------------------------

    def issue_code(self, code_challenge: str) -> str:
        code = f"code-{len(self.codes)}"
        self.codes[code] = code_challenge
        return code

    def _new_tokens(self) -> dict:
        self.token_counter += 1
        access, refresh = f"access-{self.token_counter}", f"refresh-{self.token_counter}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": 86157}

    def oauth(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/revoke":
            token = request.headers["Authorization"].removeprefix("Bearer ")
            self.revoked.append(token)
            self.valid_access.discard(token)
            return httpx.Response(200)
        assert request.url.path == "/oauth/token"
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form["code"], None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if challenge is None or computed != challenge or form["redirect_uri"] != REDIRECT_URI:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._new_tokens())
        if form["grant_type"] == "refresh_token":
            self.refresh_calls += 1
            if form["refresh_token"] not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
            self.valid_refresh.discard(form["refresh_token"])
            return httpx.Response(200, json=self._new_tokens())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    # --- API --------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "fiken.no":
            return self.oauth(request)
        self.requests.append(request)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token not in self.valid_access:
            return httpx.Response(401, json={"error": "invalid_token"})
        path = request.url.path.removeprefix("/api/v2")
        params = request.url.params

        if request.method == "GET" and path == "/companies":
            return httpx.Response(200, json=self.companies, headers={"Fiken-Api-Page-Count": "1"})
        assert path.startswith("/companies/test-as/"), path
        path = path.removeprefix("/companies/test-as")

        if request.method == "GET" and path == "/contacts":
            return httpx.Response(200, json=self.customers, headers={"Fiken-Api-Page-Count": "1"})
        if request.method == "GET" and path == "/invoices/drafts":
            return httpx.Response(200, json=[d for d in self.drafts if d["uuid"] == params["uuid"]])
        if request.method == "GET" and path == "/invoices":
            uid = params["invoiceDraftUuid"]
            return httpx.Response(200, json=[i for i in self.invoices if i["invoiceDraftUuid"] == uid])
        if request.method == "POST" and path == "/invoices/drafts":
            body = json.loads(request.content)
            if body["customerId"] in self.fail_create_for:
                return httpx.Response(400, json={"error": "invalid customer"})
            self.next_id += 1
            self.drafts.append({**body, "draftId": self.next_id, "attachments": []})
            return httpx.Response(
                201,
                headers={"Location": f"https://api.fiken.no/api/v2/companies/test-as/invoices/drafts/{self.next_id}"},
            )
        if request.method == "POST" and path.endswith("/attachments"):
            if self.fail_attachment:
                return httpx.Response(500, text="boom")
            draft_id = int(path.split("/")[3])
            draft = next(d for d in self.drafts if d["draftId"] == draft_id)
            assert b"application/pdf" in request.content
            draft["attachments"].append({"filename": "x.pdf"})
            return httpx.Response(201, headers={"Location": "x"})
        return httpx.Response(404, text=f"unexpected {request.method} {path}")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, token: str = "access-0") -> fiken_api.FikenAPI:
        async def provider(force: bool) -> str:
            return token

        return fiken_api.FikenAPI(
            "https://api.fiken.no/api/v2", provider, "test-as", transport=self.transport
        )


@pytest.fixture
def fake_fiken():
    return FakeFiken()
