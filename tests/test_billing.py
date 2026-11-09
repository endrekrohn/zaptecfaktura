from datetime import date, datetime
from decimal import Decimal

import pytest

from billing import (
    ValidationError,
    draft_uuid,
    line_total_ore,
    month_bounds,
    nok_to_ore,
    parse_nok_per_kwh,
    pdf_filename,
    pdf_sessions,
    previous_month,
    total_kwh,
)
from invoice import format_norwegian_accounting
from zaptec_api import ChargeSession


def session(start, energy, end=None, device="Lader 1"):
    return ChargeSession(
        start=datetime.fromisoformat(start), end=datetime.fromisoformat(end) if end else None,
        device_name=device, energy=energy, voided=False,
    )


def test_previous_month_wraps_year():
    assert previous_month(date(2026, 1, 10)) == (2025, 12)
    assert previous_month(date(2026, 11, 10)) == (2026, 10)


def test_month_bounds_december():
    assert month_bounds(2025, 12) == (datetime(2025, 12, 1), datetime(2026, 1, 1))


@pytest.mark.parametrize("year,month", [(2026, 0), (2026, 13), (1999, 5)])
def test_month_bounds_rejects_invalid(year, month):
    with pytest.raises(ValidationError):
        month_bounds(year, month)


def test_price_parsing_and_rounding():
    assert parse_nok_per_kwh("1,255") == Decimal("1.255")
    assert nok_to_ore(Decimal("1.255")) == 126
    assert nok_to_ore(Decimal("1.50")) == 150
    for bad in ["-1", "abc", "1000", "nan"]:
        with pytest.raises(ValidationError):
            parse_nok_per_kwh(bad)


def test_line_total_and_kwh():
    sessions = [session("2026-10-01T10:00", 1.2345), session("2026-10-02T10:00", 2)]
    assert total_kwh(sessions) == 3.234
    assert line_total_ore(3.234, 150) == 485  # 485.1 øre


def test_pdf_sessions_newest_first_in_old_shape():
    rows = pdf_sessions(
        [
            session("2026-10-01T10:00:00", 5.5, "2026-10-01T12:00:00"),
            session("2026-10-20T08:30:15.123", 7.25, None, None),
        ]
    )
    assert rows == [
        {"StartDateTime": "2026-10-20T08:30:15.123000", "EndDateTime": "N/A", "DeviceName": "N/A", "Energy": 7.25},
        {"StartDateTime": "2026-10-01T10:00:00", "EndDateTime": "2026-10-01T12:00:00", "DeviceName": "Lader 1", "Energy": 5.5},
    ]


def test_draft_uuid_is_stable_per_installation_and_month():
    a = draft_uuid("inst-1", 2026, 10)
    assert a == draft_uuid("inst-1", 2026, 10)
    assert a != draft_uuid("inst-1", 2026, 11)
    assert a != draft_uuid("inst-2", 2026, 10)


def test_pdf_filename_is_safe():
    assert pdf_filename(2026, 3, 'Gata 1/B: "Garasje"') == "2026_03_grunnlag_Gata_1_B___Garasje_.pdf"


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0,00"), (1234567.891, "1 234 567,89"), (0.995, "1,00"), (-1500.5, "-1 500,50")],
)
def test_format_norwegian_accounting(value, expected):
    assert format_norwegian_accounting(value) == expected
