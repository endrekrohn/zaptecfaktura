import asyncio
import json

import database
from billing import InstallationUsage, draft_uuid
from config import Settings
from drafts import plan_drafts, run_draft_job

SETTINGS = Settings(fiken_api_token="test-token", fiken_company_slug="test-as")


def usage(installation_id="inst-1", kwh=12.5):
    return InstallationUsage(
        installation_id=installation_id,
        name=f"Garasje {installation_id}",
        sessions=[{"Energy": kwh, "StartDateTime": "2026-10-02T10:00:00", "DeviceName": "Lader 1"}],
        total_kwh=kwh,
    )


def run(fake_fiken, usages, mappings, price_ore=150):
    items = plan_drafts(usages, mappings, 2026, 10, price_ore)
    asyncio.run(run_draft_job(fake_fiken.client(), SETTINGS, items, 2026, 10, price_ore))
    return database.get_drafts(2026, 10)


def test_creates_draft_with_attachment(fake_fiken):
    records = run(fake_fiken, [usage()], {"inst-1": 11})

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


def test_second_run_does_not_duplicate(fake_fiken):
    run(fake_fiken, [usage()], {"inst-1": 11})
    records = run(fake_fiken, [usage()], {"inst-1": 11})
    assert len(fake_fiken.drafts) == 1
    assert records["inst-1"].status == "exists"


def test_already_invoiced(fake_fiken):
    fake_fiken.invoices.append({"invoiceDraftUuid": draft_uuid("inst-1", 2026, 10)})
    records = run(fake_fiken, [usage()], {"inst-1": 11})
    assert fake_fiken.drafts == []
    assert records["inst-1"].status == "invoiced"


def test_skips_zero_usage_and_missing_customer(fake_fiken):
    records = run(fake_fiken, [usage("a", 0), usage("b", 3)], {})
    assert records["a"].status == "skipped"
    assert records["b"].status == "missing_customer"
    assert fake_fiken.requests == []


def test_failed_attachment_is_repaired_on_retry(fake_fiken):
    fake_fiken.fail_attachment = True
    records = run(fake_fiken, [usage()], {"inst-1": 11})
    assert records["inst-1"].status == "failed"
    assert "500" in records["inst-1"].error

    fake_fiken.fail_attachment = False
    records = run(fake_fiken, [usage()], {"inst-1": 11})
    assert records["inst-1"].status == "exists"
    assert len(fake_fiken.drafts) == 1
    assert len(fake_fiken.drafts[0]["attachments"]) == 1


def test_one_failure_does_not_stop_the_rest(fake_fiken):
    fake_fiken.fail_create_for = {11}
    records = run(fake_fiken, [usage("a"), usage("b")], {"a": 11, "b": 22})
    assert records["a"].status == "failed"
    assert records["b"].status == "created"
    assert [d["customerId"] for d in fake_fiken.drafts] == [22]


def test_product_line_when_configured(fake_fiken):
    from dataclasses import replace

    items = plan_drafts([usage()], {"inst-1": 11}, 2026, 10, 150)
    settings = replace(SETTINGS, fiken_product_id=77)
    asyncio.run(run_draft_job(fake_fiken.client(), settings, items, 2026, 10, 150))
    line = fake_fiken.drafts[0]["lines"][0]
    assert line["productId"] == 77
    assert "vatType" not in line and "incomeAccount" not in line


def test_requests_are_serialized_with_request_id(fake_fiken):
    run(fake_fiken, [usage()], {"inst-1": 11})
    assert all("X-Request-ID" in r.headers for r in fake_fiken.requests)
    create = next(r for r in fake_fiken.requests if r.method == "POST" and r.url.path.endswith("/drafts"))
    assert json.loads(create.content)["customerId"] == 11
