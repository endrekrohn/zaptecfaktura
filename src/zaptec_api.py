from typing import Any

import httpx

PAGE_SIZE = 100
MAX_PAGES = 1000  # guard against an API that never reports the last page


class ZaptecError(Exception):
    pass


class ZaptecAuthError(ZaptecError):
    pass


class ZaptecAPI:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, transport=self._transport, timeout=30
        )

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """Password grant. Returns the token response (access_token, expires_in)."""
        async with self._client() as client:
            response = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                },
            )
        if response.status_code != 200:
            raise ZaptecAuthError("Invalid credentials")
        return response.json()

    async def _get_all_pages(
        self, token: str, path: str, params: dict[str, Any], what: str
    ) -> list[dict[str, Any]]:
        """Fetch every page of a Zaptec paged endpoint ({Pages, Data})."""
        headers = {"Authorization": f"Bearer {token}"}
        items: list[dict[str, Any]] = []
        async with self._client() as client:
            page = 0
            while page < MAX_PAGES:
                response = await client.get(
                    path,
                    params={**params, "PageSize": PAGE_SIZE, "PageIndex": page},
                    headers=headers,
                )
                if response.status_code == 401:
                    raise ZaptecAuthError(f"Not authorized to fetch {what}")
                if response.status_code != 200:
                    raise ZaptecError(f"Failed to fetch {what}: {response.text}")
                body = response.json()
                data = body.get("Data") or []
                items.extend(data)
                pages = body.get("Pages")
                page += 1
                if pages is None:
                    # No page count: stop when a page comes back short.
                    if len(data) < PAGE_SIZE:
                        break
                elif page >= pages:
                    break
        return items

    async def get_installations(self, token: str) -> list[dict[str, Any]]:
        return await self._get_all_pages(token, "/api/installation", {}, "installations")

    async def get_chargehistory(
        self, token: str, installation_id: str, from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        return await self._get_all_pages(
            token,
            "/api/chargehistory",
            {
                "InstallationId": installation_id,
                "From": from_date,
                "To": to_date,
                "DetailLevel": 1,
            },
            "charge history",
        )
