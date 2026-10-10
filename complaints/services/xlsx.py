# ruff: noqa: E501  (the XML boilerplate below is long by nature)
"""A small .xlsx writer (standard library only): text and number cells, a styled header row,
column widths, a frozen header and filter buttons. Enough for a readable table in Excel."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELL_CHARS = 32000  # Excel's limit is 32,767


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    widths: list[int] = field(default_factory=list)


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(ref: str, value: Any, style: int) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape(_ILLEGAL.sub("", str(value))[:MAX_CELL_CHARS])
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    cols = ""
    if sheet.widths:
        cols = (
            "<cols>"
            + "".join(
                f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                for i, w in enumerate(sheet.widths, start=1)
            )
            + "</cols>"
        )
    rows = ['<row r="1" ht="22" customHeight="1">']
    rows += [_cell(f"{_column(c)}1", h, 1) for c, h in enumerate(sheet.headers)]
    rows.append("</row>")
    for r, row in enumerate(sheet.rows, start=2):
        rows.append(f'<row r="{r}">')
        rows += [_cell(f"{_column(c)}{r}", v, 2) for c, v in enumerate(row)]
        rows.append("</row>")
    last = f"{_column(max(len(sheet.headers) - 1, 0))}{max(len(sheet.rows) + 1, 1)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"{cols}<sheetData>{''.join(rows)}</sheetData>"
        f'<autoFilter ref="A1:{last}"/></worksheet>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="10"/><name val="Calibri"/></font>'
    '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF14233B"/></patternFill></fill></fills>'
    '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left/><right/><top/><bottom style="thin"><color rgb="FFD8DDE6"/></bottom><diagonal/></border>'
    "</borders>"
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" '
    'applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" '
    'applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
)


def _sheet_name(name: str, taken: set[str]) -> str:
    clean = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Sheet"
    base, n = clean, 2
    while clean.lower() in taken:
        clean = f"{base[:28]} {n}"
        n += 1
    taken.add(clean.lower())
    return clean


def write_workbook(sheets: list[Sheet]) -> bytes:
    taken: set[str] = set()
    names = [_sheet_name(s.name, taken) for s in sheets]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, n in enumerate(names, start=1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        z.writestr("xl/styles.xml", _STYLES)
        for i, sheet in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheet))
    return buffer.getvalue()
