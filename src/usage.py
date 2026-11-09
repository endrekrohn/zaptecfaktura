"""Zaptec usage with caching and bounded parallelism.

- Installations are cached in memory per user for a short while.
- Monthly totals for the overview come from Zaptec's usage report. Totals
  for months that closed a few days ago are stored in SQLite (numbers only);
  the current month is cached in memory for a few minutes.
- Session lists (for PDFs and drafts) are fetched only when needed and never
  stored. When they disagree with a cached total, the cache is corrected so
  the overview matches what is invoiced.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import database
from billing import InstallationUsage, month_bounds, total_kwh
from zaptec_api import ZaptecAPI

log = logging.getLogger(__name__)

INSTALLATIONS_TTL = 15 * 60
OPEN_MONTH_TTL = 5 * 60
# Offline sessions can arrive late; only persist a month this long after it ended.
CLOSED_AFTER = timedelta(days=5)
CONCURRENCY = 4


@dataclass
class InstallationTotal:
    installation_id: str
    name: str
    kwh: float
    session_count: int


def _name(installation: dict) -> str:
    return installation.get("Name") or installation.get("name") or _id(installation)


def _id(installation: dict) -> str:
    return installation.get("Id") or installation.get("id")


class UsageService:
    def __init__(self, zaptec: ZaptecAPI, concurrency: int = CONCURRENCY):
        self.zaptec = zaptec
        self._concurrency = concurrency
        self._installations: dict[str, tuple[float, list[dict]]] = {}
        self._open_totals: dict[tuple[str, int, int], tuple[float, float, int]] = {}

    async def _gather(self, coroutines):
        semaphore = asyncio.Semaphore(self._concurrency)

        async def limited(coroutine):
            async with semaphore:
                return await coroutine

        return await asyncio.gather(*(limited(c) for c in coroutines))

    @staticmethod
    def _is_closed(year: int, month: int, now: datetime | None = None) -> bool:
        _, end = month_bounds(year, month)
        now = now or datetime.now(UTC).replace(tzinfo=None)
        return now >= end + CLOSED_AFTER

    def forget_user(self, user: str) -> None:
        self._installations.pop(user.lower(), None)

    # --- installations ----------------------------------------------------

    async def installations(self, token: str, user: str, refresh: bool = False) -> list[dict]:
        key = user.lower()
        cached = self._installations.get(key)
        if cached and not refresh and cached[0] > time.monotonic():
            return cached[1]
        installations = await self.zaptec.get_installations(token)
        installations.sort(key=lambda i: _name(i).lower())
        self._installations[key] = (time.monotonic() + INSTALLATIONS_TTL, installations)
        return installations

    async def find_installation(self, token: str, user: str, installation_id: str) -> dict | None:
        for installation in await self.installations(token, user):
            if _id(installation) == installation_id:
                return installation
        return None

    # --- totals (overview) --------------------------------------------------

    async def totals(
        self, token: str, user: str, year: int, month: int, refresh: bool = False
    ) -> list[InstallationTotal]:
        installations = await self.installations(token, user, refresh)
        start, end = month_bounds(year, month)
        closed = self._is_closed(year, month)
        stored = {} if refresh or not closed else database.get_usage_totals(year, month)

        known: dict[str, tuple[float, int]] = {}
        missing: list[dict] = []
        for installation in installations:
            iid = _id(installation)
            open_cached = self._open_totals.get((iid, year, month))
            if iid in stored:
                known[iid] = stored[iid]
            elif not closed and not refresh and open_cached and open_cached[0] > time.monotonic():
                known[iid] = (open_cached[1], open_cached[2])
            else:
                missing.append(installation)

        fetched = await self._gather(
            self.zaptec.get_usage_total(token, _id(i), start, end) for i in missing
        )
        for installation, total in zip(missing, fetched):
            iid = _id(installation)
            known[iid] = (total.kwh, total.session_count)
            if closed:
                database.set_usage_total(iid, year, month, total.kwh, total.session_count)
            else:
                self._open_totals[(iid, year, month)] = (
                    time.monotonic() + OPEN_MONTH_TTL,
                    total.kwh,
                    total.session_count,
                )

        return [
            InstallationTotal(_id(i), _name(i), *known[_id(i)]) for i in installations
        ]

    # --- sessions (PDF and drafts) ----------------------------------------

    async def usage(self, token: str, installation: dict, year: int, month: int) -> InstallationUsage:
        start, end = month_bounds(year, month)
        iid = _id(installation)
        sessions = await self.zaptec.get_archived_sessions(token, iid, start, end)
        usage = InstallationUsage(
            installation_id=iid,
            name=_name(installation),
            sessions=sessions,
            total_kwh=total_kwh(sessions),
        )
        self._reconcile(usage, year, month)
        return usage

    async def all_usage(self, token: str, user: str, year: int, month: int) -> list[InstallationUsage]:
        installations = await self.installations(token, user)
        return list(
            await self._gather(self.usage(token, i, year, month) for i in installations)
        )

    def _reconcile(self, usage: InstallationUsage, year: int, month: int) -> None:
        """Keep the cached total in line with the sessions that are invoiced."""
        key = (usage.installation_id, year, month)
        count = len(usage.sessions)
        if self._is_closed(year, month):
            stored = database.get_usage_totals(year, month).get(usage.installation_id)
            if stored and abs(stored[0] - usage.total_kwh) > 0.0005:
                log.warning(
                    "Usage report (%.3f kWh) and sessions (%.3f kWh) differ for %s %d-%02d",
                    stored[0], usage.total_kwh, usage.installation_id, year, month,
                )
            database.set_usage_total(usage.installation_id, year, month, usage.total_kwh, count)
        else:
            self._open_totals[key] = (time.monotonic() + OPEN_MONTH_TTL, usage.total_kwh, count)
