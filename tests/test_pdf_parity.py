"""The usage PDF must be identical to what the old /api/chargehistory code
path produced for the same sessions."""

import asyncio
import random

import httpx
import pytest
from reportlab import rl_config

from drafts import usage_pdf
from invoice import generate_invoice_pdf
from usage import UsageService
from zaptec_api import ZaptecAPI

# Sessions as the old endpoint returned them: PascalCase, naive UTC times,
# newest first, including a session that started in the previous month.
OLD_SESSIONS = [
    {"StartDateTime": "2025-09-26T08:04:37.19", "EndDateTime": "2025-09-26T11:22:56.87", "DeviceName": "Lader 1", "Energy": 23.031},
    {"StartDateTime": "2025-09-24T16:14:27.44", "EndDateTime": "2025-09-25T07:55:03.89", "DeviceName": "Lader 2", "Energy": 55.573},
    {"StartDateTime": "2025-09-17T18:08:11.303", "EndDateTime": "2025-09-18T05:19:03.917", "DeviceName": "Lader 1", "Energy": 65.334},
    {"StartDateTime": "2025-09-15T15:23:56.753", "EndDateTime": "2025-09-15T18:36:51.287", "DeviceName": "Lader 2", "Energy": 22.397},
    {"StartDateTime": "2025-08-31T08:26:21.92", "EndDateTime": "2025-09-01T05:35:32.42", "DeviceName": "Lader 1", "Energy": 51.039},
]


def as_archived(old: dict) -> dict:
    """The same session as /api/sessions/archived returns it."""
    return {
        "id": "x",
        "startDateTime": old["StartDateTime"] + "Z",
        "endDateTime": old["EndDateTime"] + "Z",
        "recognizedDateTime": old["EndDateTime"] + "Z",
        "deviceName": old["DeviceName"],
        "energy": old["Energy"],
        "voided": False,
        "authorizedUser": {"email": "beboer@example.com", "fullName": "Beboer"},
        "energyDetails": [],
    }


@pytest.fixture(autouse=True)
def deterministic_pdf(monkeypatch):
    monkeypatch.setattr(rl_config, "invariant", 1)


def old_pdf(price: float) -> bytes:
    kwh = round(sum(s.get("Energy", 0) for s in OLD_SESSIONS), 3)
    return generate_invoice_pdf(
        "inst-1", "Garasje A", 2025, 9, OLD_SESSIONS, kwh, price, round(kwh * price, 2)
    )


def new_pdf(unit_price_ore: int) -> bytes:
    archived = [as_archived(s) for s in OLD_SESSIONS]
    random.Random(4).shuffle(archived)  # archive order is by recognition time
    archived.append({**as_archived(OLD_SESSIONS[0]), "voided": True, "energy": 0})

    def handler(request):
        return httpx.Response(200, json={"sessions": archived, "cursor": None, "hasMore": False})

    service = UsageService(ZaptecAPI("https://zaptec.test", transport=httpx.MockTransport(handler)))
    installation = {"Id": "inst-1", "Name": "Garasje A"}
    usage = asyncio.run(service.usage("t", installation, 2025, 9))
    return usage_pdf(usage, 2025, 9, unit_price_ore)


@pytest.mark.parametrize("ore,price", [(140, 1.40), (199, 1.99), (1, 0.01)])
def test_pdf_is_byte_identical(ore, price):
    assert new_pdf(ore) == old_pdf(price)
