"""Create Fiken invoice drafts for one month, one installation at a time."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import database
from billing import InstallationUsage, draft_uuid, pdf_filename
from config import Settings
from fiken_api import FikenAPI
from invoice import format_month_to_norwegian, generate_invoice_pdf

log = logging.getLogger(__name__)

# One job at a time, so two submits for the same month can't both see
# "no draft yet" and create duplicates.
_job_lock = asyncio.Lock()

STATUSES: dict[str, tuple[str, str]] = {
    # status: (label, status dot class)
    "pending": ("Oppretter …", "s-run"),
    "created": ("Utkast opprettet", "s-ok"),
    "exists": ("Utkast finnes i Fiken", "s-ok"),
    "invoiced": ("Fakturert i Fiken", "s-ok"),
    "skipped": ("Ingen forbruk", ""),
    "missing_customer": ("Mangler kunde", "s-warn"),
    "failed": ("Feilet", "s-err"),
}


@dataclass
class DraftJobItem:
    usage: InstallationUsage
    fiken_contact_id: int
    draft_uuid: str


def plan_drafts(
    usages: list[InstallationUsage],
    mappings: dict[str, int],
    year: int,
    month: int,
    unit_price_ore: int,
) -> list[DraftJobItem]:
    """Record a status for every installation and return the ones to create."""
    items: list[DraftJobItem] = []
    for usage in usages:
        uid = draft_uuid(usage.installation_id, year, month)
        contact_id = mappings.get(usage.installation_id)
        if usage.total_kwh <= 0:
            status = "skipped"
        elif contact_id is None:
            status = "missing_customer"
        else:
            status = "pending"
            items.append(DraftJobItem(usage, contact_id, uid))
        database.upsert_draft(
            database.DraftRecord(
                installation_id=usage.installation_id,
                year=year,
                month=month,
                draft_uuid=uid,
                fiken_contact_id=contact_id,
                kwh=usage.total_kwh,
                unit_price_ore=unit_price_ore,
                status=status,
            )
        )
    return items


def build_draft_body(
    settings: Settings,
    item: DraftJobItem,
    year: int,
    month: int,
    unit_price_ore: int,
    issue_date: date,
) -> dict[str, Any]:
    month_name = format_month_to_norwegian(month) or f"{month:02d}"
    line: dict[str, Any] = {
        "description": f"Strømforbruk elbillading {month_name} {year} (kWh)",
        "quantity": item.usage.total_kwh,
        "unitPrice": unit_price_ore,
    }
    if settings.fiken_product_id is not None:
        line["productId"] = settings.fiken_product_id
    else:
        line["vatType"] = settings.fiken_vat_type
        line["incomeAccount"] = settings.fiken_income_account
    return {
        "type": "invoice",
        "uuid": item.draft_uuid,
        "customerId": item.fiken_contact_id,
        "daysUntilDueDate": settings.fiken_days_until_due,
        "issueDate": issue_date.isoformat(),
        "invoiceText": (
            f"Strømforbruk for elbillading på {item.usage.name}, "
            f"{month_name} {year}. Se vedlagt grunnlag."
        ),
        "lines": [line],
    }


def _pdf(item: DraftJobItem, year: int, month: int, unit_price_ore: int) -> bytes:
    nok_per_kwh = unit_price_ore / 100
    return generate_invoice_pdf(
        item.usage.installation_id,
        item.usage.name,
        year,
        month,
        item.usage.sessions,
        item.usage.total_kwh,
        nok_per_kwh,
        round(item.usage.total_kwh * nok_per_kwh, 2),
    )


async def _create_one(
    fiken: FikenAPI,
    settings: Settings,
    item: DraftJobItem,
    year: int,
    month: int,
    unit_price_ore: int,
) -> tuple[str, int | None]:
    """Create the draft unless Fiken already has it. Returns (status, draft id)."""
    if await fiken.find_invoice_from_draft(item.draft_uuid):
        return "invoiced", None

    filename = pdf_filename(year, month, item.usage.name)
    existing = await fiken.find_draft(item.draft_uuid)
    if existing:
        # Never overwrite a draft the user may have edited. Only add the PDF
        # if an earlier run failed between creating the draft and attaching it.
        draft_id = existing["draftId"]
        if not existing.get("attachments"):
            pdf = _pdf(item, year, month, unit_price_ore)
            await fiken.add_draft_attachment(draft_id, filename, pdf)
        return "exists", draft_id

    body = build_draft_body(settings, item, year, month, unit_price_ore, date.today())
    draft_id = await fiken.create_invoice_draft(body)
    pdf = _pdf(item, year, month, unit_price_ore)
    await fiken.add_draft_attachment(draft_id, filename, pdf)
    return "created", draft_id


async def run_draft_job(
    fiken: FikenAPI,
    settings: Settings,
    items: list[DraftJobItem],
    year: int,
    month: int,
    unit_price_ore: int,
) -> None:
    async with _job_lock:
        for item in items:
            record = database.DraftRecord(
                installation_id=item.usage.installation_id,
                year=year,
                month=month,
                draft_uuid=item.draft_uuid,
                fiken_contact_id=item.fiken_contact_id,
                kwh=item.usage.total_kwh,
                unit_price_ore=unit_price_ore,
                status="pending",
            )
            try:
                status, draft_id = await _create_one(
                    fiken, settings, item, year, month, unit_price_ore
                )
                record.status = status
                record.fiken_draft_id = draft_id
            except Exception as e:  # keep going with the next installation
                log.exception(
                    "Draft for installation %s failed", item.usage.installation_id
                )
                record.status = "failed"
                record.error = str(e)[:500]
            database.upsert_draft(record)
