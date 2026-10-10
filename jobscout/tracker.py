"""The Application Tracker PDF: every job I have applied to, newest first.

Applications stay on the dashboard until I mark them followed up; after that
they live only in the archive, and this PDF is where I keep track of them.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from fpdf import FPDF
from fpdf.fonts import FontFace

# The built-in PDF fonts only cover Latin-1, so map common typography first.
_REPLACEMENTS = str.maketrans(
    {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "•": "-", "…": "...", "→": "->"}
)
_COLUMNS = ("Company", "Role", "Location", "Applied", "Reach out by", "Status", "Notes")
_WIDTHS = (34, 62, 40, 22, 22, 30, 46)  # millimetres, sums to the printable width
_HEADER = FontFace(emphasis="BOLD", fill_color=(31, 42, 68), color=(255, 255, 255))
_LINK_COLOR = (23, 92, 211)


def latin1(text: str) -> str:
    """Text the built-in PDF fonts can draw, keeping accents where Latin-1 has them."""
    cleaned = (text or "").translate(_REPLACEMENTS)
    if all(ord(char) < 256 for char in cleaned):
        return cleaned
    decomposed = unicodedata.normalize("NFKD", cleaned)
    return "".join(
        char if ord(char) < 256 else "?" for char in decomposed if not unicodedata.combining(char)
    )


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status(row: sqlite3.Row, follow_up_due: datetime | None, now: datetime) -> str:
    if row["status"] == "follow_up" or row["archived_at"]:
        return "Followed up"
    if follow_up_due and follow_up_due <= now:
        return "Reach out now"
    return "Waiting"


def render(
    rows: Sequence[sqlite3.Row], follow_up_after: timedelta, now: datetime | None = None
) -> bytes:
    """Build the tracker from ``JobStore.applications()`` rows."""
    moment = now or datetime.now(timezone.utc)
    pdf = FPDF(orientation="landscape", format="letter")
    pdf.set_title("Application Tracker")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(h=10, text="Application Tracker", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(102, 112, 133)
    pdf.cell(
        text=f"{len(rows)} application{'s' if len(rows) != 1 else ''}. Updated {moment:%B %d, %Y}.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(4)
    pdf.set_text_color(0, 0, 0)

    if not rows:
        pdf.set_font("Helvetica", size=12)
        pdf.cell(text="No applications yet. Mark jobs Applied in the dashboard to track them here.")
        return bytes(pdf.output())

    pdf.set_font("Helvetica", size=9)
    with pdf.table(
        col_widths=_WIDTHS,
        headings_style=_HEADER,
        line_height=5,
        text_align="LEFT",
        cell_fill_color=(247, 248, 250),
        cell_fill_mode="ROWS",
    ) as table:
        header = table.row()
        for column in _COLUMNS:
            header.cell(column)
        for row in rows:
            applied = _date(row["applied_at"])
            due = applied + follow_up_after if applied else None
            cells = table.row()
            cells.cell(latin1(row["company"]))
            cells.cell(
                latin1(row["title"]),
                link=row["url"] or None,
                style=FontFace(color=_LINK_COLOR) if row["url"] else None,
            )
            cells.cell(latin1(row["location"] or ""))
            cells.cell(f"{applied:%b %d, %Y}" if applied else "")
            cells.cell(f"{due:%b %d, %Y}" if due else "")
            cells.cell(_status(row, due, moment))
            cells.cell(latin1(row["notes"] or ""))
    return bytes(pdf.output())
