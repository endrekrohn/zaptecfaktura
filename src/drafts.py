"""Create Fiken invoice drafts for one month, one installation at a time.

The request only plans the work (from the overview totals) and returns. The
background job then fetches each installation's sessions from Zaptec,
generates the PDF and creates the draft, so the invoice line and the
attachment are always built from the same sessions.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import database
from billing import InstallationUsage, draft_uuid, pdf_filename, pdf_sessions
from fiken_api import FikenAPI
from fiken_oauth import FikenNotConnected
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

# VAT types Fiken accepts on sales lines, with labels for the settings page.
SALES_VAT_TYPES: dict[str, str] = {
    "HIGH": "Høy sats (25 %)",
    "MEDIUM": "Middels sats (15 %)",
    "LOW": "Lav sats (12 %)",
    "RAW_FISH": "Råfisk (11,11 %)",
    "NONE": "Ingen mva-behandling",
    "EXEMPT": "Fritatt for mva",
    "OUTSIDE": "Utenfor avgiftsområdet",
    "EXEMPT_IMPORT_EXPORT": "Fritatt, utførsel av varer og tjenester",
    "EXEMPT_REVERSE": "Fritatt, omvendt avgiftsplikt",
}

# Sales income accounts in the Norwegian standard chart of accounts.
INCOME_ACCOUNT_RANGE = (3000, 3999)


@dataclass(frozen=True)
class InvoiceSettings:
    """Per-company settings for the invoice line, set under /fiken/innstillinger.
    The defaults apply until a company saves its own."""

    income_account: str = "3000"
    vat_type: str = "HIGH"
    days_until_due: int = 14


# Statuses where the draft exists in Fiken and can be opened there.
OPENABLE_STATUSES = {"created", "exists"}


def draft_url(fiken_web_url: str, company_slug: str, draft_uuid: str) -> str:
    """Link to the draft in Fiken. Fiken addresses drafts by the UUID we set."""
    return f"{fiken_web_url.rstrip('/')}/foretak/{company_slug}/fakturautkast/{draft_uuid}"


# Fetches one installation's sessions for the month.
UsageLoader = Callable[[str], Awaitable[InstallationUsage]]


@dataclass
class DraftJobItem:
    installation_id: str
    name: str
    kwh: float  # from the overview; the job uses the sessions' sum
    fiken_contact_id: int
    draft_uuid: str


@dataclass
class PlannedInstallation:
    installation_id: str
    name: str
    kwh: float


def plan_drafts(
    company_slug: str,
    installations: list[PlannedInstallation],
    mappings: dict[str, int],
    year: int,
    month: int,
    unit_price_ore: int,
) -> list[DraftJobItem]:
    """Record a status for every installation and return the ones to create."""
    items: list[DraftJobItem] = []
    for inst in installations:
        uid = draft_uuid(inst.installation_id, year, month)
        contact_id = mappings.get(inst.installation_id)
        if inst.kwh <= 0:
            status = "skipped"
        elif contact_id is None:
            status = "missing_customer"
        else:
            status = "pending"
            items.append(DraftJobItem(inst.installation_id, inst.name, inst.kwh, contact_id, uid))
        database.upsert_draft(
            database.DraftRecord(
                company_slug=company_slug,
                installation_id=inst.installation_id,
                year=year,
                month=month,
                draft_uuid=uid,
                fiken_contact_id=contact_id,
                kwh=inst.kwh,
                unit_price_ore=unit_price_ore,
                status=status,
            )
        )
    return items


def build_draft_body(
    invoice: InvoiceSettings,
    item: DraftJobItem,
    usage: InstallationUsage,
    year: int,
    month: int,
    unit_price_ore: int,
    issue_date: date,
) -> dict[str, Any]:
    month_name = format_month_to_norwegian(month) or f"{month:02d}"
    line: dict[str, Any] = {
        "description": f"Strømforbruk elbillading {month_name} {year} (kWh)",
        "quantity": usage.total_kwh,
        "unitPrice": unit_price_ore,
        "vatType": invoice.vat_type,
        "incomeAccount": invoice.income_account,
    }
    return {
        "type": "invoice",
        "uuid": item.draft_uuid,
        "customerId": item.fiken_contact_id,
        "daysUntilDueDate": invoice.days_until_due,
        "issueDate": issue_date.isoformat(),
        "invoiceText": (
            f"Strømforbruk for elbillading på {usage.name}, "
            f"{month_name} {year}. Se vedlagt grunnlag."
        ),
        "lines": [line],
    }


def usage_pdf(usage: InstallationUsage, year: int, month: int, unit_price_ore: int) -> bytes:
    """The usage PDF (fakturagrunnlag). Same layout and numbers as before."""
    nok_per_kwh = unit_price_ore / 100
    return generate_invoice_pdf(
        usage.installation_id,
        usage.name,
        year,
        month,
        pdf_sessions(usage.sessions),
        usage.total_kwh,
        nok_per_kwh,
        round(usage.total_kwh * nok_per_kwh, 2),
    )


async def _create_one(
    fiken: FikenAPI,
    invoice: InvoiceSettings,
    item: DraftJobItem,
    load_usage: UsageLoader,
    year: int,
    month: int,
    unit_price_ore: int,
) -> tuple[str, int | None, float | None]:
    """Create the draft unless Fiken already has it.

    Returns (status, draft id, invoiced kWh).
    """
    if await fiken.find_invoice_from_draft(item.draft_uuid):
        return "invoiced", None, None

    existing = await fiken.find_draft(item.draft_uuid)
    if existing:
        # Never overwrite a draft the user may have edited. Only add the PDF
        # if an earlier run failed between creating the draft and attaching it.
        draft_id = existing["draftId"]
        if not existing.get("attachments"):
            usage = await load_usage(item.installation_id)
            pdf = usage_pdf(usage, year, month, unit_price_ore)
            await fiken.add_draft_attachment(
                draft_id, pdf_filename(year, month, usage.name), pdf
            )
        return "exists", draft_id, None

    usage = await load_usage(item.installation_id)
    if usage.total_kwh <= 0:
        return "skipped", None, usage.total_kwh
    body = build_draft_body(invoice, item, usage, year, month, unit_price_ore, date.today())
    draft_id = await fiken.create_invoice_draft(body)
    pdf = usage_pdf(usage, year, month, unit_price_ore)
    await fiken.add_draft_attachment(draft_id, pdf_filename(year, month, usage.name), pdf)
    return "created", draft_id, usage.total_kwh


async def run_draft_job(
    fiken: FikenAPI,
    invoice: InvoiceSettings,
    company_slug: str,
    items: list[DraftJobItem],
    load_usage: UsageLoader,
    year: int,
    month: int,
    unit_price_ore: int,
) -> None:
    not_connected = "Fiken-tilkoblingen er borte. Koble til Fiken på nytt og prøv igjen."
    lost_connection = False
    async with _job_lock:
        for item in items:
            record = database.DraftRecord(
                company_slug=company_slug,
                installation_id=item.installation_id,
                year=year,
                month=month,
                draft_uuid=item.draft_uuid,
                fiken_contact_id=item.fiken_contact_id,
                kwh=item.kwh,
                unit_price_ore=unit_price_ore,
                status="pending",
            )
            if lost_connection:
                record.status, record.error = "failed", not_connected
                database.upsert_draft(record)
                continue
            try:
                status, draft_id, kwh = await _create_one(
                    fiken, invoice, item, load_usage, year, month, unit_price_ore
                )
                record.status = status
                record.fiken_draft_id = draft_id
                if kwh is not None:
                    record.kwh = kwh
            except FikenNotConnected:
                # Consent revoked mid-run: the rest would fail the same way.
                lost_connection = True
                record.status, record.error = "failed", not_connected
            except Exception as e:  # keep going with the next installation
                log.exception("Draft for installation %s failed", item.installation_id)
                record.status = "failed"
                record.error = str(e)[:500]
            database.upsert_draft(record)
