"""Minimal Fiken API v2 client for invoice drafts.

Fiken allows only one concurrent request per client and slows clients that
send more than 4 requests per second, so every request goes through one lock
and is spaced at least MIN_INTERVAL seconds apart.
"""

import asyncio
import time
import uuid
from typing import Any

import httpx

MIN_INTERVAL = 0.3
MAX_RETRIES = 3
PAGE_SIZE = 100


class FikenError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FikenAPI:
    def __init__(
        self,
        base_url: str,
        token: str,
        company_slug: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.company_path = f"/companies/{company_slug}"
        self._token = token
        self._transport = transport
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Request-ID": str(uuid.uuid4()),
            **kwargs.pop("headers", {}),
        }
        async with self._lock:
            for attempt in range(MAX_RETRIES + 1):
                wait = self._last_request + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                async with httpx.AsyncClient(
                    base_url=self.base_url, transport=self._transport, timeout=30
                ) as client:
                    response = await client.request(
                        method, path, headers=headers, **kwargs
                    )
                self._last_request = time.monotonic()
                if response.status_code == 429 and attempt < MAX_RETRIES:
                    await asyncio.sleep(2**attempt)
                    continue
                break
        if response.status_code >= 400:
            raise FikenError(
                f"Fiken svarte {response.status_code} på {method} {path}: "
                f"{response.text[:500]}",
                response.status_code,
            )
        return response

    @staticmethod
    def _id_from_location(response: httpx.Response) -> int:
        location = response.headers.get("Location", "")
        try:
            return int(location.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            raise FikenError(f"Mangler id i Location-header: {location!r}") from None

    # --- contacts ---------------------------------------------------------

    async def list_customers(self) -> list[dict[str, Any]]:
        customers: list[dict[str, Any]] = []
        page = 0
        while True:
            response = await self._request(
                "GET",
                f"{self.company_path}/contacts",
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
            "GET", f"{self.company_path}/invoices/drafts", params={"uuid": draft_uuid}
        )
        drafts = response.json()
        return drafts[0] if drafts else None

    async def find_invoice_from_draft(self, draft_uuid: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"{self.company_path}/invoices",
            params={"invoiceDraftUuid": draft_uuid},
        )
        invoices = response.json()
        return invoices[0] if invoices else None

    async def create_invoice_draft(self, body: dict[str, Any]) -> int:
        response = await self._request(
            "POST", f"{self.company_path}/invoices/drafts", json=body
        )
        return self._id_from_location(response)

    async def add_draft_attachment(
        self, draft_id: int, filename: str, content: bytes
    ) -> None:
        await self._request(
            "POST",
            f"{self.company_path}/invoices/drafts/{draft_id}/attachments",
            data={"filename": filename},
            files={"file": (filename, content, "application/pdf")},
        )
