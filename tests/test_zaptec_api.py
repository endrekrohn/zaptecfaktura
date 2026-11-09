import asyncio
import json
from datetime import datetime

import httpx
import pytest

from zaptec_api import ChargeSession, ZaptecAPI, ZaptecAuthError, parse_time

START, END = datetime(2026, 10, 1), datetime(2026, 11, 1)


def archived(i, **overrides):
    item = {
        "id": f"s{i}",
        "startDateTime": f"2026-10-{i:02d}T08:00:00.1234567Z",
        "endDateTime": f"2026-10-{i:02d}T10:00:00Z",
        "deviceName": "Lader 1",
        "energy": float(i),
        "voided": False,
        "authorizedUser": {"id": "u1", "email": "beboer@example.com", "fullName": "Beboer"},
        "tokenName": "RFID 1",
        "energyDetails": [{"timestamp": "2026-10-01T08:15:00Z", "energy": 0.4}],
    }
    item.update(overrides)
    return item


def test_parse_time_variants():
    assert parse_time("2025-09-26T08:04:37.19") == datetime(2025, 9, 26, 8, 4, 37, 190000)
    assert parse_time("2025-09-26T08:04:37.1234567Z") == datetime(2025, 9, 26, 8, 4, 37, 123456)
    assert parse_time("2025-09-26T10:04:37+02:00") == datetime(2025, 9, 26, 8, 4, 37)
    assert parse_time(None) is None


def test_archived_sessions_follow_cursor_and_drop_voided_and_user_data():
    pages = {
        None: {"sessions": [archived(1), archived(2, voided=True, energy=0)], "cursor": "c1", "hasMore": True},
        "c1": {"sessions": [archived(3)], "cursor": None, "hasMore": False},
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions/archived"
        params = request.url.params
        assert params["InstallationId"] == "inst-1"
        assert params["From"] == "2026-10-01T00:00:00Z" and params["To"] == "2026-11-01T00:00:00Z"
        assert params["PageSize"] == "200"
        seen.append(params.get("Cursor"))
        return httpx.Response(200, json=pages[params.get("Cursor")])

    api = ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler))
    sessions = asyncio.run(api.get_archived_sessions("t", "inst-1", START, END))
    assert seen == [None, "c1"]
    assert [s.energy for s in sessions] == [1.0, 3.0]
    assert sessions[0] == ChargeSession(
        start=datetime(2026, 10, 1, 8, 0, 0, 123456),
        end=datetime(2026, 10, 1, 10, 0),
        device_name="Lader 1",
        energy=1.0,
        voided=False,
    )
    # Only these fields are kept: no user name, email or RFID token.
    assert set(ChargeSession.__dataclass_fields__) == {"start", "end", "device_name", "energy", "voided"}


def test_pascal_case_responses_are_understood():
    def handler(request):
        return httpx.Response(
            200,
            json={"Sessions": [{"StartDateTime": "2026-10-05T08:00:00", "Energy": 4.5, "DeviceName": "L"}], "HasMore": False},
        )

    api = ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler))
    [s] = asyncio.run(api.get_archived_sessions("t", "inst-1", START, END))
    assert s.energy == 4.5 and s.start == datetime(2026, 10, 5, 8)


def test_usage_report_sums_groups():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/chargehistory/installationreport"
        body = json.loads(request.content)
        assert body == {
            "fromDate": "2026-10-01T00:00:00Z",
            "endDate": "2026-11-01T00:00:00Z",
            "installationId": "inst-1",
            "groupBy": 1,
            "reportFormat": 0,
        }
        return httpx.Response(
            200,
            json={
                "installationName": "Garasje",
                "totalUserChargerReportModel": [
                    {"groupAsString": "L1", "totalChargeSessionCount": 3, "totalChargeSessionEnergy": 10.1234},
                    {"groupAsString": "L2", "totalChargeSessionCount": 2, "totalChargeSessionEnergy": 5.0},
                ],
            },
        )

    api = ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler))
    total = asyncio.run(api.get_usage_total("t", "inst-1", START, END))
    assert total.kwh == 15.123 and total.session_count == 5


def test_installations_are_owned_and_paged():
    calls = []

    def handler(request):
        params = request.url.params
        calls.append(params["PageIndex"])
        assert params["Roles"] == "2"  # owner
        index = int(params["PageIndex"])
        return httpx.Response(200, json={"Pages": 2, "Data": [{"Id": f"i{index}"}]})

    api = ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler))
    assert [i["Id"] for i in asyncio.run(api.get_installations("t"))] == ["i0", "i1"]
    assert calls == ["0", "1"]


def test_expired_token_raises_auth_error():
    api = ZaptecAPI(
        "https://zaptec.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(ZaptecAuthError):
        asyncio.run(api.get_installations("expired"))


def test_deprecated_chargehistory_is_not_used():
    import inspect

    import zaptec_api

    source = inspect.getsource(zaptec_api)
    assert '"/api/chargehistory"' not in source
    assert "DetailLevel" not in source
