import asyncio

import httpx
import pytest

from zaptec_api import PAGE_SIZE, ZaptecAPI, ZaptecAuthError


def test_chargehistory_reads_every_page():
    pages = {0: [{"Energy": 1.0}] * PAGE_SIZE, 1: [{"Energy": 2.0}] * 5}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params["PageIndex"])
        seen.append(index)
        assert request.url.params["InstallationId"] == "inst-1"
        return httpx.Response(200, json={"Pages": 2, "Data": pages[index]})

    api = ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler))
    sessions = asyncio.run(api.get_chargehistory("t", "inst-1", "a", "b"))
    assert seen == [0, 1]
    assert len(sessions) == PAGE_SIZE + 5


def test_expired_token_raises_auth_error():
    api = ZaptecAPI(
        "https://zaptec.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(ZaptecAuthError):
        asyncio.run(api.get_installations("expired"))
