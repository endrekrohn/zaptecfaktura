import asyncio
import json
from dataclasses import replace
from datetime import datetime

import database
from billing import InstallationUsage, draft_uuid
from config import Settings
from drafts import PlannedInstallation, plan_drafts, run_draft_job
from zaptec_api import ChargeSession

SETTINGS = Settings()


def planned(installation_id="inst-1", kwh=12.5):
    return PlannedInstallation(installation_id, f"Garasje {installation_id}", kwh)


def make_loader(kwh_by_installation=None, calls=None):
    async def load(installation_id):
        if calls is not None:
            calls.append(installation_id)
        kwh = (kwh_by_installation or {}).get(installation_id, 12.5)
        sessions = [ChargeSession(datetime(2026, 10, 2, 10), datetime(2026, 10, 2, 12), "Lader 1", kwh, False)]
        return InstallationUsage(installation_id, f"Garasje {installation_id}", sessions, kwh)

    return load


def run(fake_fiken, items_in, mappings, price_ore=150, loader=None, settings=SETTINGS, client=None):
    items = plan_drafts("test-as", items_in, mappings, 2026, 10, price_ore)
    asyncio.run(
        run_draft_job(
            client or fake_fiken.client(), settings, "test-as", items,
            loader or make_loader(), 2026, 10, price_ore,
        )
    )
    return database.get_drafts("test-as", 2026, 10)


def test_creates_draft_with_attachment(fake_fiken):
    records = run(fake_fiken, [planned()], {"inst-1": 11})

    assert records["inst-1"].status == "created"
    [draft] = fake_fiken.drafts
    assert draft["uuid"] == draft_uuid("inst-1", 2026, 10)
    assert draft["customerId"] == 11
    assert draft["type"] == "invoice"
    assert draft["daysUntilDueDate"] == 14
    assert draft["lines"] == [
        {
            "description": "Strømforbruk elbillading oktober 2026 (kWh)",
            "quantity": 12.5,
            "unitPrice": 150,
            "vatType": "HIGH",
            "incomeAccount": "3000",
        }
    ]
    assert len(draft["attachments"]) == 1
    assert records["inst-1"].fiken_draft_id == draft["draftId"]


def test_invoice_uses_session_sum_not_overview_total(fake_fiken):
    records = run(fake_fiken, [planned(kwh=12.0)], {"inst-1": 11}, loader=make_loader({"inst-1": 12.75}))
    assert fake_fiken.drafts[0]["lines"][0]["quantity"] == 12.75
    assert records["inst-1"].kwh == 12.75


def test_second_run_does_not_duplicate_or_refetch(fake_fiken):
    run(fake_fiken, [planned()], {"inst-1": 11})
    calls = []
    records = run(fake_fiken, [planned()], {"inst-1": 11}, loader=make_loader(calls=calls))
    assert len(fake_fiken.drafts) == 1
    assert records["inst-1"].status == "exists"
    assert calls == []  # existing draft with attachment: no Zaptec call


def test_already_invoiced(fake_fiken):
    fake_fiken.invoices.append({"invoiceDraftUuid": draft_uuid("inst-1", 2026, 10)})
    records = run(fake_fiken, [planned()], {"inst-1": 11})
    assert fake_fiken.drafts == []
    assert records["inst-1"].status == "invoiced"


def test_skips_zero_usage_and_missing_customer(fake_fiken):
    records = run(fake_fiken, [planned("a", 0), planned("b", 3)], {})
    assert records["a"].status == "skipped"
    assert records["b"].status == "missing_customer"
    assert fake_fiken.requests == []


def test_sessions_with_no_energy_are_skipped(fake_fiken):
    records = run(fake_fiken, [planned()], {"inst-1": 11}, loader=make_loader({"inst-1": 0}))
    assert records["inst-1"].status == "skipped"
    assert fake_fiken.drafts == []


def test_failed_attachment_is_repaired_on_retry(fake_fiken):
    fake_fiken.fail_attachment = True
    records = run(fake_fiken, [planned()], {"inst-1": 11})
    assert records["inst-1"].status == "failed"
    assert "500" in records["inst-1"].error

    fake_fiken.fail_attachment = False
    records = run(fake_fiken, [planned()], {"inst-1": 11})
    assert records["inst-1"].status == "exists"
    assert len(fake_fiken.drafts) == 1
    assert len(fake_fiken.drafts[0]["attachments"]) == 1


def test_one_failure_does_not_stop_the_rest(fake_fiken):
    fake_fiken.fail_create_for = {11}
    records = run(fake_fiken, [planned("a"), planned("b")], {"a": 11, "b": 22})
    assert records["a"].status == "failed"
    assert records["b"].status == "created"
    assert [d["customerId"] for d in fake_fiken.drafts] == [22]


def test_product_line_when_configured(fake_fiken):
    run(fake_fiken, [planned()], {"inst-1": 11}, settings=replace(SETTINGS, fiken_product_id=77))
    line = fake_fiken.drafts[0]["lines"][0]
    assert line["productId"] == 77
    assert "vatType" not in line and "incomeAccount" not in line


def test_requests_carry_request_id(fake_fiken):
    run(fake_fiken, [planned()], {"inst-1": 11})
    assert all("X-Request-ID" in r.headers for r in fake_fiken.requests)
    create = next(r for r in fake_fiken.requests if r.method == "POST" and r.url.path.endswith("/drafts"))
    assert json.loads(create.content)["customerId"] == 11


def test_lost_connection_fails_remaining_items(fake_fiken):
    from fiken_api import FikenAPI
    from fiken_oauth import FikenNotConnected

    async def provider(force):
        raise FikenNotConnected("revoked")

    client = FikenAPI("https://api.fiken.no/api/v2", provider, "test-as", transport=fake_fiken.transport)
    records = run(fake_fiken, [planned("a"), planned("b")], {"a": 11, "b": 22}, client=client)
    assert {r.status for r in records.values()} == {"failed"}
    assert "Koble til Fiken" in records["b"].error
