"""Build finished DOCX and XLSX files from Agent output with the standard library only.

A coworker that can only reply with text has not finished the job (#101). This module turns structured output (a
titled report with headings and paragraphs, or sheets of rows) into a real Office file, deterministically (fixed
zip timestamps, so the same input gives the same bytes and digest), and hands it to the Tier 0 artifact store with
the sources it was built from. It is intentionally small: no styling, images, charts or templates, and **no PPTX,
PDF or native Google/Microsoft formats yet**. Nothing here is wired into a Skill or the governed sidecar.

Safety choices:

- **Text stays text.** Every spreadsheet string is written as an inline string, never a formula, so a value such as
  ``=HYPERLINK(...)`` from an email cannot execute when the file is opened; numbers are written as numbers. CSV
  output prefixes cells that start with ``= + - @`` with a quote for the same reason.
- **Valid XML only.** Control characters are removed and ``& < > "`` are escaped in every text node and attribute.
- **Bounded.** Documents, sheets, rows and cells have size limits so a runaway generation cannot fill the store.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_TYPE = "text/csv"
MAX_SECTIONS = 200
MAX_PARAGRAPHS = 2000
MAX_PARAGRAPH = 20000
MAX_SHEETS = 20
MAX_ROWS = 10000
MAX_COLUMNS = 100
MAX_CELL = 32767
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class DeliverableError(ValueError):
    """Raised when the requested content is invalid or too large."""


def _text(value: Any) -> str:
    return _CONTROL.sub("", value if isinstance(value, str) else str(value))


def _escape(value: Any) -> str:
    return _text(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _zip(parts: Sequence[tuple[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()


_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def build_docx(title: str, sections: Sequence[Mapping[str, Any]]) -> bytes:
    """A report: a title and sections of ``{"heading": str, "paragraphs": [str, ...]}``."""
    if not isinstance(title, str) or not _text(title).strip():
        raise DeliverableError("a document needs a title")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)) or not sections or len(sections) > MAX_SECTIONS:
        raise DeliverableError(f"a document needs 1 to {MAX_SECTIONS} sections")

    def paragraph(text: str, style: str | None = None) -> str:
        properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f'<w:p>{properties}<w:r><w:t xml:space="preserve">{_escape(text)}</w:t></w:r></w:p>'

    body = [paragraph(title.strip(), "Title")]
    count = 0
    for section in sections:
        if not isinstance(section, Mapping):
            raise DeliverableError("each section is an object with a heading and paragraphs")
        heading, paragraphs = section.get("heading"), section.get("paragraphs", [])
        if heading is not None:
            if not isinstance(heading, str) or not _text(heading).strip():
                raise DeliverableError("a section heading must be a non-empty string")
            body.append(paragraph(heading.strip(), "Heading1"))
        if not isinstance(paragraphs, Sequence) or isinstance(paragraphs, (str, bytes)):
            raise DeliverableError("paragraphs must be a list of strings")
        for text in paragraphs:
            if not isinstance(text, str) or len(text) > MAX_PARAGRAPH:
                raise DeliverableError(f"a paragraph is a string of at most {MAX_PARAGRAPH} characters")
            count += 1
            if count > MAX_PARAGRAPHS:
                raise DeliverableError(f"a document has at most {MAX_PARAGRAPHS} paragraphs")
            body.append(paragraph(text))
    document = (
        _XML + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(body) + "</w:body></w:document>"
    )
    return _zip([
        ("[Content_Types].xml", _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         "</Types>"),
        ("_rels/.rels", _XML + f'<Relationships xmlns="{_REL_NS}"><Relationship Id="rId1" '
         f'Type="{_OFFICE_REL}/officeDocument" Target="word/document.xml"/></Relationships>'),
        ("word/document.xml", document),
    ])


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def build_xlsx(sheets: Sequence[Mapping[str, Any]]) -> bytes:
    """A spreadsheet: sheets of ``{"name": str, "rows": [[cell, ...], ...]}``; cells are strings, numbers, booleans or empty."""
    if not isinstance(sheets, Sequence) or isinstance(sheets, (str, bytes)) or not sheets or len(sheets) > MAX_SHEETS:
        raise DeliverableError(f"a workbook needs 1 to {MAX_SHEETS} sheets")
    names: list[str] = []
    rendered: list[str] = []
    for sheet in sheets:
        name = sheet.get("name") if isinstance(sheet, Mapping) else None
        if not isinstance(name, str) or not 1 <= len(name) <= 31 or _BAD_SHEET.search(name) or name != name.strip("'") or _CONTROL.search(name):
            raise DeliverableError("a sheet name is 1 to 31 characters without [ ] : * ? / \\ or edge quotes")
        if name.lower() in {item.lower() for item in names}:
            raise DeliverableError("sheet names must be unique")
        names.append(name)
        rows = sheet.get("rows", [])
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or len(rows) > MAX_ROWS:
            raise DeliverableError(f"a sheet has at most {MAX_ROWS} rows")
        row_xml = []
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) > MAX_COLUMNS:
                raise DeliverableError(f"a row is a list of at most {MAX_COLUMNS} cells")
            cells = []
            for column_number, value in enumerate(row):
                reference = f"{_column(column_number)}{row_number}"
                if value is None or value == "":
                    continue
                if isinstance(value, bool):
                    cells.append(f'<c r="{reference}" t="b"><v>{int(value)}</v></c>')
                elif isinstance(value, (int, float)):
                    if value != value or value in (float("inf"), float("-inf")):
                        raise DeliverableError("a number cell must be finite")
                    cells.append(f'<c r="{reference}"><v>{value!r}</v></c>')
                elif isinstance(value, str):
                    if len(value) > MAX_CELL:
                        raise DeliverableError(f"a cell is at most {MAX_CELL} characters")
                    cells.append(f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{_escape(value)}</t></is></c>')
                else:
                    raise DeliverableError("a cell is a string, number, boolean or empty")
            row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        rendered.append(
            _XML + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + "".join(row_xml) + "</sheetData></worksheet>"
        )
    count = len(names)
    parts = [
        ("[Content_Types].xml", _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
         + "".join(f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   for n in range(1, count + 1)) + "</Types>"),
        ("_rels/.rels", _XML + f'<Relationships xmlns="{_REL_NS}"><Relationship Id="rId1" '
         f'Type="{_OFFICE_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        ("xl/workbook.xml", _XML + f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{_OFFICE_REL}"><sheets>'
         + "".join(f'<sheet name="{_escape(name)}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(names, start=1)) + "</sheets></workbook>"),
        ("xl/_rels/workbook.xml.rels", _XML + f'<Relationships xmlns="{_REL_NS}">'
         + "".join(f'<Relationship Id="rId{n}" Type="{_OFFICE_REL}/worksheet" Target="worksheets/sheet{n}.xml"/>' for n in range(1, count + 1))
         + "</Relationships>"),
    ]
    parts.extend((f"xl/worksheets/sheet{n}.xml", xml) for n, xml in enumerate(rendered, start=1))
    return _zip(parts)


def build_csv(rows: Sequence[Sequence[Any]]) -> bytes:
    """CSV with spreadsheet-formula injection neutralised (cells starting ``= + - @`` get a leading quote)."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or len(rows) > MAX_ROWS:
        raise DeliverableError(f"a CSV has at most {MAX_ROWS} rows")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) > MAX_COLUMNS:
            raise DeliverableError(f"a row is a list of at most {MAX_COLUMNS} cells")
        writer.writerow([
            ("'" + _text(cell)) if isinstance(cell, str) and cell.startswith(_FORMULA_START) else _text("" if cell is None else cell)
            for cell in row
        ])
    return buffer.getvalue().encode("utf-8")


def deliver(
    store: Any,
    *,
    kind: str,
    name: str,
    content: Mapping[str, Any],
    agent: str,
    requested_by: str,
    sources: Sequence[str] = (),
    classification: str = "internal",
    now: int | None = None,
) -> dict[str, Any]:
    """Build a ``docx``, ``xlsx`` or ``csv`` deliverable and store it, ledgering its location, digest and sources.

    ``content`` is ``{"title", "sections"}`` for a docx, ``{"sheets"}`` for an xlsx and ``{"rows"}`` for a csv.
    The result is the artifact record (with ``location``), never the bytes.
    """
    try:
        if kind == "docx":
            data, media = build_docx(content.get("title", ""), content.get("sections", [])), DOCX_TYPE
        elif kind == "xlsx":
            data, media = build_xlsx(content.get("sheets", [])), XLSX_TYPE
        elif kind == "csv":
            data, media = build_csv(content.get("rows", [])), CSV_TYPE
        else:
            raise DeliverableError("kind must be docx, xlsx or csv; PPTX and PDF are not generated yet")
    except AttributeError as exc:
        raise DeliverableError("content must be an object") from exc
    suffix = {"docx": ".docx", "xlsx": ".xlsx", "csv": ".csv"}[kind]
    label = name if name.lower().endswith(suffix) else name + suffix
    return store.put(name=label, content=data, media_type=media, agent=agent, requested_by=requested_by,
                     sources=sources, classification=classification, now=now)
