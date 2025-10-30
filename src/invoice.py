import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def generate_invoice_pdf(
    installation_id: str,
    installation_name: str | None,
    year: str,
    month: int,
    sessions_data: list,
    total_kwh: float | int,
    nok_per_kwh: float | int,
    total_cost: float | int,
):
    buffer = io.BytesIO()
    page_width, page_height = A4

    left_margin = 30
    right_margin = 30
    top_margin = 30
    bottom_margin = 30
    content_width = page_width - left_margin - right_margin
    content_height = page_height - top_margin - bottom_margin

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontSize=18,
        spaceAfter=30,
        alignment=0,
    )

    heading_style = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=12,
        spaceAfter=15,
        alignment=0,
    )
    elements = []

    title = Paragraph(
        f"Fakturagrunnlag ({installation_name or installation_id})", title_style
    )
    elements.append(title)

    period = f"For periode {year}-{month:02d} er den gjennomsnittlige strømprisne inkludert påslag satt til {nok_per_kwh:.2f} NOK per kWh."
    elements.append(Paragraph(period, styles["Normal"]))

    elements.append(Spacer(1, 20))

    line_items = [["Beskrivelse", "Antall", "Pris", "Rabatt", "MVA", "Beløp"]]
    line_items.append(
        [
            f"Strømforbruk {year}-{month:02d}",
            f"{total_kwh:.3f}",
            f"{nok_per_kwh:.2f}",
            f"{0:.0f} %",
            f"{0:.0f} %",
            f"{format_norwegian_accounting(total_cost)} kr",
        ]
    )
    line_items.append(
        [
            "",
            "",
            "",
            "",
            "Nettobeløp",
            f"{format_norwegian_accounting(total_cost)} kr",
        ]
    )
    line_items.append(
        [
            "",
            "",
            "",
            "",
            "Merverdiavgift",
            f"{format_norwegian_accounting(0)} kr",
        ]
    )
    line_items.append(
        [
            "",
            "",
            "",
            "",
            "Beløp å betale",
            f"{format_norwegian_accounting(total_cost)} kr",
        ]
    )
    line_table = Table(
        line_items,
        colWidths=[
            content_width * (4 / 12),
            content_width * (1 / 12),
            content_width * (1 / 6),
            content_width * (1 / 12),
            content_width * (1 / 12),
            content_width * (3 / 12),
        ],
    )
    line_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                # ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEABOVE", (0, -3), (-1, -3), 1, colors.black),
                ("LINEABOVE", (-3, -1), (-1, -1), 1, colors.black),
                ("LINEBELOW", (-3, -1), (-1, -1), 1.5, colors.black),
                (
                    "TEXTCOLOR",
                    (0, -1),
                    (-1, -1),
                    colors.black,
                ),
                (
                    "FONTNAME",
                    (0, -1),
                    (-1, -1),
                    "Helvetica-Bold",
                ),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
                (
                    "ALIGN",
                    (0, 0),
                    (0, -1),
                    "LEFT",
                ),
                ("FONTSIZE", (0, -1), (-1, -1), 12),
            ]
        )
    )
    elements.append(line_table)

    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Detaljert strømforbruk for perioden", heading_style))

    if sessions_data:
        usage_headers = [
            "Starttidspunkt",
            "Sluttidspunkt",
            "Ladeenhet",
            "Strømforbruk (kWh)",
        ]
        usage_data = [usage_headers]

        for session in sessions_data:
            start_time = session.get("StartDateTime", "N/A")
            end_time = session.get("EndDateTime", "N/A")

            try:
                if start_time != "N/A":
                    dt = datetime.fromisoformat(start_time)
                    start_time = dt.strftime("%Y-%m-%d %H:%M")
                if end_time != "N/A":
                    dt = datetime.fromisoformat(end_time)
                    end_time = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

            row = [
                start_time,
                end_time,
                session.get("DeviceName", "N/A"),
                str(session.get("Energy", 0)),
            ]
            usage_data.append(row)

        usage_data.append(["Sum kWh", "", "", f"{total_kwh:.3f}"])
        usage_table = Table(
            usage_data,
            colWidths=[
                0.25 * content_width,
                0.25 * content_width,
                0.25 * content_width,
                0.25 * content_width,
            ],
        )
        usage_table.setStyle(
            TableStyle(
                [
                    # ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                    # ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
                    ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
                    (
                        "TEXTCOLOR",
                        (0, -1),
                        (-1, -1),
                        colors.black,
                    ),
                    (
                        "FONTNAME",
                        (0, -1),
                        (-1, -1),
                        "Helvetica-Bold",
                    ),
                    ("FONTSIZE", (0, -1), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
                    (
                        "ALIGN",
                        (-1, 0),
                        (-1, -1),
                        "RIGHT",
                    ),
                ]
            )
        )
        elements.append(usage_table)
    else:
        elements.append(
            Paragraph("Ingen ladesesjoner funnet for denne perioden.", styles["Normal"])
        )

    doc.build(elements)

    buffer.seek(0)
    pdf_bytes = buffer.read()
    buffer.close()

    return pdf_bytes


def format_norwegian_accounting(number):
    """
    Format a number according to Norwegian accounting conventions.

    Args:
        number (float or int): The number to format

    Returns:
        str: Formatted string in Norwegian accounting style
    """
    number = float(number)

    integer_part = int(number)
    decimal_part = round((number - integer_part) * 100)

    is_negative = integer_part < 0
    if is_negative:
        integer_part = abs(integer_part)

    int_str = str(integer_part)

    formatted_int = ""
    for i, digit in enumerate(reversed(int_str)):
        if i > 0 and i % 3 == 0:
            formatted_int = " " + formatted_int
        formatted_int = digit + formatted_int

    if decimal_part < 10:
        decimal_str = f"0{decimal_part}"
    else:
        decimal_str = str(decimal_part)

    result = f"{formatted_int},{decimal_str}"

    if is_negative:
        result = f"-{result}"

    return result
