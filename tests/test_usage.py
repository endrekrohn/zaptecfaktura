import asyncio
from datetime import datetime

import database
from usage import UsageService
from zaptec_api import ChargeSession, UsageTotal


class CountingZaptec:
    def __init__(self):
        self.calls = {"installations": 0, "totals": 0, "sessions": 0}
        self.kwh = 10.0
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_installations(self, token):
        self.calls["installations"] += 1
        return [{"Id": f"i{n}", "Name": f"Inst {n}"} for n in (3, 1, 2)]

    async def get_usage_total(self, token, installation_id, start, end):
        self.calls["totals"] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        return UsageTotal(self.kwh, 2)

    async def get_archived_sessions(self, token, installation_id, start, end):
        self.calls["sessions"] += 1
        return [ChargeSession(datetime(2025, 9, 2), None, "L", 6.0, False), ChargeSession(datetime(2025, 9, 3), None, "L", 6.5, False)]


def test_installations_cached_and_sorted():
    zaptec = CountingZaptec()
    service = UsageService(zaptec)
    first = asyncio.run(service.installations("t", "U@x.no"))
    asyncio.run(service.installations("t", "u@x.no"))
    assert [i["Name"] for i in first] == ["Inst 1", "Inst 2", "Inst 3"]
    assert zaptec.calls["installations"] == 1
    asyncio.run(service.installations("t", "u@x.no", refresh=True))
    assert zaptec.calls["installations"] == 2


def test_closed_month_totals_are_stored_and_fetched_in_parallel():
    zaptec = CountingZaptec()
    service = UsageService(zaptec, concurrency=2)
    totals = asyncio.run(service.totals("t", "u", 2025, 9))
    assert [(t.name, t.kwh, t.session_count) for t in totals] == [("Inst 1", 10.0, 2), ("Inst 2", 10.0, 2), ("Inst 3", 10.0, 2)]
    assert zaptec.calls["totals"] == 3
    assert zaptec.max_in_flight == 2

    # A new service (e.g. after restart) reads from SQLite.
    other = UsageService(zaptec)
    asyncio.run(other.totals("t", "u", 2025, 9))
    assert zaptec.calls["totals"] == 3
    assert database.get_usage_totals(2025, 9)["i1"] == (10.0, 2)

    asyncio.run(other.totals("t", "u", 2025, 9, refresh=True))
    assert zaptec.calls["totals"] == 6


def test_open_month_is_not_persisted():
    zaptec = CountingZaptec()
    service = UsageService(zaptec)
    now = datetime.now()
    asyncio.run(service.totals("t", "u", now.year, now.month))
    asyncio.run(service.totals("t", "u", now.year, now.month))
    assert zaptec.calls["totals"] == 3  # cached in memory
    assert database.get_usage_totals(now.year, now.month) == {}


def test_session_sum_corrects_cached_total():
    zaptec = CountingZaptec()
    service = UsageService(zaptec)
    asyncio.run(service.totals("t", "u", 2025, 9))
    usage = asyncio.run(service.usage("t", {"Id": "i1", "Name": "Inst 1"}, 2025, 9))
    assert usage.total_kwh == 12.5
    assert database.get_usage_totals(2025, 9)["i1"] == (12.5, 2)
