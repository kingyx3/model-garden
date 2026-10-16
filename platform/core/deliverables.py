"""Build finished DOCX, XLSX, PPTX and PDF files from Agent output with the standard library only.

A coworker that can only reply with text has not finished the job (#101). This module turns structured output (a
titled report with headings and paragraphs, sheets of rows, or a slide deck) into a real file, deterministically
(fixed zip timestamps and no PDF dates, so the same input gives the same bytes and digest), and hands it to the Tier 0
artifact store with the sources it was built from. It is intentionally small: no styling, images, charts, templates,
hyperlinks, macros or scripts, and **no native Google/Microsoft formats yet**. Nothing here is wired into a Skill or the
governed sidecar.

Safety choices:

- **Text stays text.** Every spreadsheet string is written as an inline string, never a formula, so a value such as
  ``=HYPERLINK(...)`` from an email cannot execute when the file is opened; numbers are written as numbers. CSV
  output prefixes cells that start with ``= + - @`` with a quote for the same reason.
- **Valid XML only.** Control characters are removed and ``& < > "`` are escaped in every text node and attribute.
- **Bounded.** Documents, sheets, rows, cells, slides and PDF pages have size limits so a runaway generation cannot fill
  the store.
- **Passive files.** A PPTX holds only text shapes and a PDF only text drawing operators: no hyperlinks, actions,
  JavaScript, embedded files or macros, so text taken from an email cannot do anything when the file is opened.
- **Not claimed to be checked in a real viewer by the tests.** The tests parse the package or the PDF structure; opening
  the files in PowerPoint, Word or a PDF reader is evidence still to be gathered per client.
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
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF_TYPE = "application/pdf"
CSV_TYPE = "text/csv"
MAX_SLIDES = 100
MAX_BULLETS = 20
MAX_BULLET = 1000
MAX_PDF_PAGES = 200
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


_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_THEME = (
    _XML + f'<a:theme xmlns:a="{_A_NS}" name="Model Garden"><a:themeElements>'
    '<a:clrScheme name="Plain"><a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="1F2937"/></a:dk2><a:lt2><a:srgbClr val="F3F4F6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="2563EB"/></a:accent1><a:accent2><a:srgbClr val="059669"/></a:accent2>'
    '<a:accent3><a:srgbClr val="D97706"/></a:accent3><a:accent4><a:srgbClr val="DC2626"/></a:accent4>'
    '<a:accent5><a:srgbClr val="7C3AED"/></a:accent5><a:accent6><a:srgbClr val="0891B2"/></a:accent6>'
    '<a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="7C3AED"/></a:folHlink></a:clrScheme>'
    '<a:fontScheme name="Plain"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Plain"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
    '<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
    '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
    '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
)


def _placeholder(shape_id: int, name: str, kind: str, index: int | None, box: tuple[int, int, int, int] | None, paragraphs: str) -> str:
    idx = f' idx="{index}"' if index is not None else ""
    geometry = f'<a:xfrm><a:off x="{box[0]}" y="{box[1]}"/><a:ext cx="{box[2]}" cy="{box[3]}"/></a:xfrm>' if box else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f'<p:nvPr><p:ph type="{kind}"{idx}/></p:nvPr></p:nvSpPr><p:spPr>{geometry}</p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>'
    )


def _runs(text: str, size: int | None = None) -> str:
    attributes = f' sz="{size}"' if size else ""
    return f'<a:p><a:r><a:rPr lang="en-US"{attributes}/><a:t>{_escape(text)}</a:t></a:r></a:p>'


def build_pptx(title: str, slides: Sequence[Mapping[str, Any]]) -> bytes:
    """A deck: a title slide, then slides of ``{"title": str, "bullets": [str, ...]}`` (text only, widescreen 16:9)."""
    if not isinstance(title, str) or not _text(title).strip():
        raise DeliverableError("a deck needs a title")
    if not isinstance(slides, Sequence) or isinstance(slides, (str, bytes)) or not slides or len(slides) > MAX_SLIDES:
        raise DeliverableError(f"a deck needs 1 to {MAX_SLIDES} slides after the title")
    width, height = 12192000, 6858000
    title_box, body_box = (609600, 365760, 10972800, 1143000), (609600, 1600200, 10972800, 4525963)
    empty_group = (
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    )
    rendered = [
        f'<p:sld xmlns:a="{_A_NS}" xmlns:r="{_OFFICE_REL}" xmlns:p="{_P_NS}"><p:cSld><p:spTree>{empty_group}'
        + _placeholder(2, "Title", "title", None, (609600, 2286000, 10972800, 1371600), _runs(title.strip(), 4400)) + "</p:spTree></p:cSld></p:sld>"
    ]
    for slide in slides:
        if not isinstance(slide, Mapping):
            raise DeliverableError("each slide is an object with a title and bullets")
        heading, bullets = slide.get("title"), slide.get("bullets", [])
        if not isinstance(heading, str) or not _text(heading).strip():
            raise DeliverableError("a slide needs a title")
        if not isinstance(bullets, Sequence) or isinstance(bullets, (str, bytes)) or len(bullets) > MAX_BULLETS:
            raise DeliverableError(f"bullets is a list of at most {MAX_BULLETS} strings")
        lines = []
        for bullet in bullets:
            if not isinstance(bullet, str) or len(bullet) > MAX_BULLET:
                raise DeliverableError(f"a bullet is a string of at most {MAX_BULLET} characters")
            lines.append(_runs(bullet))
        body = "".join(lines) or '<a:p><a:endParaRPr lang="en-US"/></a:p>'
        rendered.append(
            f'<p:sld xmlns:a="{_A_NS}" xmlns:r="{_OFFICE_REL}" xmlns:p="{_P_NS}"><p:cSld><p:spTree>{empty_group}'
            + _placeholder(2, "Title", "title", None, None, _runs(heading.strip()))
            + _placeholder(3, "Content", "body", 1, None, body) + "</p:spTree></p:cSld></p:sld>"
        )
    count = len(rendered)
    master = (
        _XML + f'<p:sldMaster xmlns:a="{_A_NS}" xmlns:r="{_OFFICE_REL}" xmlns:p="{_P_NS}"><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
        f'<p:spTree>{empty_group}'
        + _placeholder(2, "Title Placeholder", "title", None, title_box, '<a:p><a:r><a:rPr lang="en-US"/><a:t>Title</a:t></a:r></a:p>')
        + _placeholder(3, "Text Placeholder", "body", 1, body_box, '<a:p><a:r><a:rPr lang="en-US"/><a:t>Text</a:t></a:r></a:p>')
        + '</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
        'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="3200" b="1"/></a:lvl1pPr></p:titleStyle>'
        '<p:bodyStyle><a:lvl1pPr marL="342900" indent="-342900"><a:buChar char="&#8226;"/><a:defRPr sz="2000"/></a:lvl1pPr></p:bodyStyle>'
        '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>'
    )
    layout = (
        _XML + f'<p:sldLayout xmlns:a="{_A_NS}" xmlns:r="{_OFFICE_REL}" xmlns:p="{_P_NS}" type="obj" preserve="1"><p:cSld name="Title and Content">'
        f'<p:spTree>{empty_group}'
        + _placeholder(2, "Title", "title", None, None, '<a:p><a:r><a:rPr lang="en-US"/><a:t>Title</a:t></a:r></a:p>')
        + _placeholder(3, "Content", "body", 1, None, '<a:p><a:r><a:rPr lang="en-US"/><a:t>Text</a:t></a:r></a:p>')
        + '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
    )
    layout_rel = f'<Relationship Id="rId1" Type="{_OFFICE_REL}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
    parts = [
        ("[Content_Types].xml", _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
         '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
         '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
         '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
         + "".join(f'<Override PartName="/ppt/slides/slide{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                   for n in range(1, count + 1)) + "</Types>"),
        ("_rels/.rels", _XML + f'<Relationships xmlns="{_REL_NS}"><Relationship Id="rId1" '
         f'Type="{_OFFICE_REL}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'),
        ("ppt/presentation.xml", _XML + f'<p:presentation xmlns:a="{_A_NS}" xmlns:r="{_OFFICE_REL}" xmlns:p="{_P_NS}">'
         '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>'
         + "".join(f'<p:sldId id="{255 + n}" r:id="rId{n + 1}"/>' for n in range(1, count + 1))
         + f'</p:sldIdLst><p:sldSz cx="{width}" cy="{height}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'),
        ("ppt/_rels/presentation.xml.rels", _XML + f'<Relationships xmlns="{_REL_NS}">'
         f'<Relationship Id="rId1" Type="{_OFFICE_REL}/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
         + "".join(f'<Relationship Id="rId{n + 1}" Type="{_OFFICE_REL}/slide" Target="slides/slide{n}.xml"/>' for n in range(1, count + 1))
         + f'<Relationship Id="rId{count + 2}" Type="{_OFFICE_REL}/theme" Target="theme/theme1.xml"/></Relationships>'),
        ("ppt/slideMasters/slideMaster1.xml", master),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", _XML + f'<Relationships xmlns="{_REL_NS}">'
         f'<Relationship Id="rId1" Type="{_OFFICE_REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
         f'<Relationship Id="rId2" Type="{_OFFICE_REL}/theme" Target="../theme/theme1.xml"/></Relationships>'),
        ("ppt/slideLayouts/slideLayout1.xml", layout),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _XML + f'<Relationships xmlns="{_REL_NS}">{layout_rel}</Relationships>'),
        ("ppt/theme/theme1.xml", _THEME),
    ]
    for number, xml in enumerate(rendered, start=1):
        parts.append((f"ppt/slides/slide{number}.xml", _XML + xml))
        parts.append((f"ppt/slides/_rels/slide{number}.xml.rels", _XML + f'<Relationships xmlns="{_REL_NS}">'
                      f'<Relationship Id="rId1" Type="{_OFFICE_REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'))
    return _zip(parts)


_PDF_WIDTH, _PDF_HEIGHT, _PDF_MARGIN = 595, 842, 56
_PDF_CHAR = 0.56  # a conservative average advance, in ems, for Helvetica; lines may wrap a little early, never overflow


def _pdf_text(value: str) -> str:
    """Latin-1 text with the characters a PDF string treats specially escaped; anything else becomes ``?``."""
    cleaned = _CONTROL.sub("", value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    encoded = cleaned.encode("latin-1", errors="replace").decode("latin-1")
    return encoded.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, size: float) -> list[str]:
    limit = max(10, int((_PDF_WIDTH - 2 * _PDF_MARGIN) / (size * _PDF_CHAR)))
    lines: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > limit:  # a word longer than a line is broken rather than run off the page
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:limit])
            word = word[limit:]
        candidate = f"{line} {word}".strip()
        if len(candidate) > limit:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines or [""]


def build_pdf(title: str, sections: Sequence[Mapping[str, Any]]) -> bytes:
    """A report as a PDF: the same ``title`` and ``sections`` as a DOCX, in Helvetica, wrapped and paginated (A4)."""
    if not isinstance(title, str) or not _text(title).strip():
        raise DeliverableError("a document needs a title")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)) or not sections or len(sections) > MAX_SECTIONS:
        raise DeliverableError(f"a document needs 1 to {MAX_SECTIONS} sections")
    blocks: list[tuple[str, float, float, str]] = [("F2", 20, 28, title.strip())]
    count = 0
    for section in sections:
        if not isinstance(section, Mapping):
            raise DeliverableError("each section is an object with a heading and paragraphs")
        heading, paragraphs = section.get("heading"), section.get("paragraphs", [])
        if heading is not None:
            if not isinstance(heading, str) or not _text(heading).strip():
                raise DeliverableError("a section heading must be a non-empty string")
            blocks.append(("F2", 14, 22, heading.strip()))
        if not isinstance(paragraphs, Sequence) or isinstance(paragraphs, (str, bytes)):
            raise DeliverableError("paragraphs must be a list of strings")
        for text in paragraphs:
            if not isinstance(text, str) or len(text) > MAX_PARAGRAPH:
                raise DeliverableError(f"a paragraph is a string of at most {MAX_PARAGRAPH} characters")
            count += 1
            if count > MAX_PARAGRAPHS:
                raise DeliverableError(f"a document has at most {MAX_PARAGRAPHS} paragraphs")
            blocks.append(("F1", 11, 15, text))
    pages: list[list[str]] = [[]]
    y = _PDF_HEIGHT - _PDF_MARGIN
    for font, size, leading, text in blocks:
        gap = 6 if font == "F1" else 4
        for line in _wrap(text, size):
            if y - leading < _PDF_MARGIN:
                pages.append([])
                y = _PDF_HEIGHT - _PDF_MARGIN
            y -= leading
            pages[-1].append(f"BT /{font} {size:g} Tf {_PDF_MARGIN} {y:g} Td ({_pdf_text(line)}) Tj ET")
            if len(pages) > MAX_PDF_PAGES:
                raise DeliverableError(f"a PDF has at most {MAX_PDF_PAGES} pages")
        y -= gap
    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{5 + 2 * number} 0 R" for number in range(len(pages)))
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for number, lines in enumerate(pages):
        stream = "\n".join(lines)
        add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PDF_WIDTH} {_PDF_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * number} 0 R >>")
        add(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    start = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    output += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode("ascii")
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode("ascii")
    return bytes(output)


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
    """Build a ``docx``, ``xlsx``, ``pptx``, ``pdf`` or ``csv`` deliverable and store it, ledgering its location, digest and sources.

    ``content`` is ``{"title", "sections"}`` for a docx or pdf, ``{"title", "slides"}`` for a pptx, ``{"sheets"}`` for an xlsx
    and ``{"rows"}`` for a csv.
    The result is the artifact record (with ``location``), never the bytes.
    """
    try:
        if kind == "docx":
            data, media = build_docx(content.get("title", ""), content.get("sections", [])), DOCX_TYPE
        elif kind == "xlsx":
            data, media = build_xlsx(content.get("sheets", [])), XLSX_TYPE
        elif kind == "pptx":
            data, media = build_pptx(content.get("title", ""), content.get("slides", [])), PPTX_TYPE
        elif kind == "pdf":
            data, media = build_pdf(content.get("title", ""), content.get("sections", [])), PDF_TYPE
        elif kind == "csv":
            data, media = build_csv(content.get("rows", [])), CSV_TYPE
        else:
            raise DeliverableError("kind must be docx, xlsx, pptx, pdf or csv")
    except AttributeError as exc:
        raise DeliverableError("content must be an object") from exc
    suffix = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf", "csv": ".csv"}[kind]
    label = name if name.lower().endswith(suffix) else name + suffix
    return store.put(name=label, content=data, media_type=media, agent=agent, requested_by=requested_by,
                     sources=sources, classification=classification, now=now)
