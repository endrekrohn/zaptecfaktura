"""Minimal Fiken API v2 client for invoice drafts.

Fiken allows only one concurrent request per client and slows clients that
send more than 4 requests per second, so every request from this app goes
through one shared lock and is spaced at least MIN_INTERVAL seconds apart.

The access token comes from a provider (the user's OAuth connection). On 401
the provider is asked for a fresh token once and the request is retried.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

MIN_INTERVAL = 0.3
MAX_RETRIES = 3
PAGE_SIZE = 100


class FikenError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


TokenProvider = Callable[[bool], Awaitable[str]]


class FikenAPI:
    # Shared by all instances: the limit applies to the app, not to a user.
    _lock = asyncio.Lock()
    _last_request = 0.0

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        company_slug: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.company_path = f"/companies/{company_slug}" if company_slug else None
        self._token_provider = token_provider
        self._transport = transport

    async def _send(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": str(uuid.uuid4()),
        }
        cls = type(self)
        async with cls._lock:
            for attempt in range(MAX_RETRIES + 1):
                wait = cls._last_request + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                async with httpx.AsyncClient(
                    base_url=self.base_url, transport=self._transport, timeout=30
                ) as client:
                    response = await client.request(method, path, headers=headers, **kwargs)
                cls._last_request = time.monotonic()
                if response.status_code == 429 and attempt < MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                return response
        return response

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = await self._send(method, path, await self._token_provider(False), **kwargs)
        if response.status_code == 401:
            # Token revoked or expired early: refresh once and retry.
            response = await self._send(method, path, await self._token_provider(True), **kwargs)
        if response.status_code >= 400:
            raise FikenError(
                f"Fiken svarte {response.status_code} på {method} {path}: "
                f"{response.text[:500]}",
                response.status_code,
            )
        return response

    def _company(self) -> str:
        if self.company_path is None:
            raise FikenError("Fiken company is not selected")
        return self.company_path

    @staticmethod
    def _id_from_location(response: httpx.Response) -> int:
        location = response.headers.get("Location", "")
        try:
            return int(location.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            raise FikenError(f"Mangler id i Location-header: {location!r}") from None

    # --- companies --------------------------------------------------------

    async def list_companies(self) -> list[dict[str, Any]]:
        companies: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._request(
                "GET", "/companies", params={"page": page, "pageSize": PAGE_SIZE}
            )
            companies.extend(response.json())
            page_count = int(response.headers.get("Fiken-Api-Page-Count", "1"))
            page += 1
            if page >= page_count:
                return companies

    # --- contacts ---------------------------------------------------------

    async def list_customers(self) -> list[dict[str, Any]]:
        customers: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._request(
                "GET",
                f"{self._company()}/contacts",
                params={"customer": "true", "page": page, "pageSize": PAGE_SIZE},
            )
            customers.extend(response.json())
            page_count = int(response.headers.get("Fiken-Api-Page-Count", "1"))
            page += 1
            if page >= page_count:
                return customers

    # --- invoice drafts ---------------------------------------------------

    async def find_draft(self, draft_uuid: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET", f"{self._company()}/invoices/drafts", params={"uuid": draft_uuid}
        )
        drafts = response.json()
        return drafts[0] if drafts else None

    async def find_invoice_from_draft(self, draft_uuid: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"{self._company()}/invoices",
            params={"invoiceDraftUuid": draft_uuid},
        )
        invoices = response.json()
        return invoices[0] if invoices else None

    async def create_invoice_draft(self, body: dict[str, Any]) -> int:
        response = await self._request(
            "POST", f"{self._company()}/invoices/drafts", json=body
        )
        return self._id_from_location(response)

    async def add_draft_attachment(
        self, draft_id: int, filename: str, content: bytes
    ) -> None:
        await self._request(
            "POST",
            f"{self._company()}/invoices/drafts/{draft_id}/attachments",
            data={"filename": filename},
            files={"file": (filename, content, "application/pdf")},
        )
