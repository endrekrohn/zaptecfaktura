import json

import httpx
import pytest

import database
import fiken_api


@pytest.fixture(autouse=True)
def db(tmp_path):
    database.init_db(str(tmp_path / "test.db"))
    yield


@pytest.fixture(autouse=True)
def no_fiken_delay(monkeypatch):
    monkeypatch.setattr(fiken_api, "MIN_INTERVAL", 0)


class FakeFiken:
    """In-memory stand-in for the Fiken endpoints the app uses."""

    def __init__(self):
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert request.headers["Authorization"] == "Bearer test-token"
        assert path.startswith("/api/v2/companies/test-as/")
        path = path.removeprefix("/api/v2/companies/test-as")
        params = request.url.params

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

    def client(self) -> fiken_api.FikenAPI:
        return fiken_api.FikenAPI(
            "https://api.fiken.no/api/v2",
            "test-token",
            "test-as",
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def fake_fiken():
    return FakeFiken()
