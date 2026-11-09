"""Zaptec API client.

Uses the endpoints that replace the deprecated GET /api/chargehistory
(sunset 2027-01-01):

- GET  /api/sessions/archived: completed sessions for one installation,
  filtered on session end time, cursor-paginated (max 200 per page).
- POST /api/chargehistory/installationreport: server-side totals (kWh and
  session count) for one installation, used for the overview.

Both require the owner role on the installation, so only owned
installations are listed. One HTTP client is reused for all requests.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

INSTALLATION_PAGE_SIZE = 100
SESSION_PAGE_SIZE = 200
MAX_PAGES = 1000  # guard against an API that never reports the last page
ROLE_OWNER = 2
REPORT_GROUP_BY_CHARGER = 1
REPORT_FORMAT_SUMMARY = 0


class ZaptecError(Exception):
    pass


class ZaptecAuthError(ZaptecError):
    pass


def field(obj: dict[str, Any], name: str, default: Any = None) -> Any:
    """Read a field regardless of casing (Zaptec answers in PascalCase while
    the OpenAPI spec documents camelCase)."""
    if name in obj:
        return obj[name]
    lower = name.lower()
    for key, value in obj.items():
        if key.lower() == lower:
            return value
    return default


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(value: str | None) -> datetime | None:
    """Parse a Zaptec timestamp to a naive UTC datetime.

    Naive values are taken as UTC, which is what the old endpoint returned.
    Fractions longer than microseconds (.NET ticks) are truncated.
    """
    if not value:
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True)
class ChargeSession:
    """The parts of a session the app uses. User details (name, email,
    RFID token) are deliberately not kept."""

    start: datetime | None
    end: datetime | None
    device_name: str | None
    energy: float
    voided: bool

    @classmethod
    def from_archived(cls, data: dict[str, Any]) -> "ChargeSession":
        return cls(
            start=parse_time(field(data, "startDateTime")),
            end=parse_time(field(data, "endDateTime")),
            device_name=field(data, "deviceName"),
            energy=float(field(data, "energy") or 0),
            voided=bool(field(data, "voided", False)),
        )


@dataclass(frozen=True)
class UsageTotal:
    kwh: float
    session_count: int


def _iso_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class ZaptecAPI:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport,
                timeout=httpx.Timeout(30, connect=10),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(self, token: str, method: str, path: str, what: str, **kwargs) -> Any:
        response = await self._client().request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 401:
            raise ZaptecAuthError(f"Not authorized to fetch {what}")
        if response.status_code != 200:
            raise ZaptecError(
                f"Failed to fetch {what} ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """Password grant. Returns the token response (access_token, expires_in)."""
        response = await self._client().post(
            "/oauth/token",
            data={"grant_type": "password", "username": username, "password": password},
        )
        if response.status_code != 200:
            raise ZaptecAuthError("Invalid credentials")
        return response.json()

    async def get_installations(self, token: str) -> list[dict[str, Any]]:
        """All installations the user owns (index-paginated)."""
        items: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            body = await self._send(
                token,
                "GET",
                "/api/installation",
                "installations",
                params={
                    "Roles": ROLE_OWNER,
                    "PageSize": INSTALLATION_PAGE_SIZE,
                    "PageIndex": page,
                },
            )
            data = field(body, "data") or []
            items.extend(data)
            pages = field(body, "pages")
            if (pages is None and len(data) < INSTALLATION_PAGE_SIZE) or (
                pages is not None and page + 1 >= pages
            ):
                break
        return items

    async def get_archived_sessions(
        self, token: str, installation_id: str, start: datetime, end: datetime
    ) -> list[ChargeSession]:
        """Completed sessions that ended in [start, end) (UTC), voided excluded."""
        sessions: list[ChargeSession] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "InstallationId": installation_id,
                "From": _iso_utc(start),
                "To": _iso_utc(end),
                "PageSize": SESSION_PAGE_SIZE,
            }
            if cursor:
                params["Cursor"] = cursor
            body = await self._send(
                token, "GET", "/api/sessions/archived", "charging sessions", params=params
            )
            for item in field(body, "sessions") or []:
                session = ChargeSession.from_archived(item)
                if not session.voided:
                    sessions.append(session)
            cursor = field(body, "cursor")
            if not field(body, "hasMore") or not cursor:
                break
        return sessions

    async def get_usage_total(
        self, token: str, installation_id: str, start: datetime, end: datetime
    ) -> UsageTotal:
        """Total kWh and session count for one installation, summed by Zaptec."""
        body = await self._send(
            token,
            "POST",
            "/api/chargehistory/installationreport",
            "usage report",
            json={
                "fromDate": _iso_utc(start),
                "endDate": _iso_utc(end),
                "installationId": installation_id,
                "groupBy": REPORT_GROUP_BY_CHARGER,
                "reportFormat": REPORT_FORMAT_SUMMARY,
            },
        )
        groups = field(body, "totalUserChargerReportModel") or []
        kwh = sum(float(field(g, "totalChargeSessionEnergy") or 0) for g in groups)
        count = sum(int(field(g, "totalChargeSessionCount") or 0) for g in groups)
        return UsageTotal(kwh=round(kwh, 3), session_count=count)
