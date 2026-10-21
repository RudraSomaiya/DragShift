"""Spreadsheet converters (PRD §6.1 spreadsheet row): XLSX CSV TSV ODS.

- Values only: formulas arrive as their last computed values, and formatting is not kept.
- The grid is copied as it is: the first row is data like any other (no header guessing).
- A workbook with several sheets becomes one CSV/TSV per sheet, ``<stem> <sheet>.csv``.
- CSV/TSV are read with the ``csv`` module, so ragged rows and ``;`` files (European Excel)
  work. Numbers in them become numbers in XLSX/ODS, except ones Excel would mangle (leading
  zeros, more than 15 digits). CSV/TSV are written as UTF-8 with a BOM, which Excel needs to
  read accented text.

pandas and its Excel engines are imported inside the functions: the UI process imports this
module to register Ops (CLAUDE.md rule 2).
"""

import csv
import datetime as dt
import io
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.registry import op

if TYPE_CHECKING:
    from pandas import DataFrame

    from dragshift.engine.worker import JobContext

SHEET = frozenset({Kind.SHEET})
_BOOK_ENGINES = {"xlsx": "openpyxl", "ods": "odf"}
_DELIMITERS = ",;\t|"
_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?")
_MAX_DIGITS = 15  # Excel keeps 15 significant digits; longer numbers (card numbers, IDs) stay text
_SHEET_NAME_BAD = re.compile(r"[\[\]:*?/\\]")


# --- reading ---------------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # what Excel writes on Western Windows


def _delimiter(text: str, ext: str) -> str:
    if ext == "tsv":
        return "\t"
    try:
        return csv.Sniffer().sniff(text[:65536], delimiters=_DELIMITERS).delimiter
    except csv.Error:  # rows of different lengths, or one column: count in the first line
        first = next((line for line in text.splitlines() if line.strip()), "")
        best = max(_DELIMITERS, key=first.count)
        return best if first.count(best) else ","


def number(cell: str) -> Any:
    """``"42"`` -> 42 and ``"3.5"`` -> 3.5; anything else (``"007"``, ``"1 234"``) stays text."""
    s = cell.strip()
    m = _NUMBER.fullmatch(s)
    digits = len(m[1]) + len(m[2] or "") - (1 if m and m[2] else 0) if m else 0
    if not m or digits > _MAX_DIGITS:
        return cell
    return float(s) if m[2] or m[3] else int(s)


def sheet_name(name: str) -> str:
    """A valid Excel sheet name: no ``[]:*?/\\``, at most 31 characters, not empty."""
    return _SHEET_NAME_BAD.sub("_", name)[:31].strip("'") or "Sheet1"


def _read_text(src: Path) -> "DataFrame":
    import pandas as pd

    text = _decode(src.read_bytes())
    rows = list(
        csv.reader(
            io.StringIO(text, newline=""), delimiter=_delimiter(text, normalize_ext(src.suffix))
        )
    )
    return pd.DataFrame([[number(c) for c in row] for row in rows])


def read_book(src: Path) -> dict[str, "DataFrame"]:
    """Every sheet of ``src``, in order, as a grid with no header row."""
    if normalize_ext(src.suffix) in ("csv", "tsv"):
        return {sheet_name(src.stem): _read_text(src)}
    import pandas as pd

    # The engine follows the file type: openpyxl (xlsx), xlrd (xls), odf (ods).
    # dtype=object: every cell as the file stored it, so the text "007" does not become 7.
    return pd.read_excel(src, sheet_name=None, header=None, dtype=object)


# --- writing ---------------------------------------------------------------------------------
def text_cell(value: Any) -> Any:
    """A cell as CSV text should show it: no ``nan``, no ``3.0``, no ``00:00:00`` on dates."""
    import pandas as pd

    if value is None or value is pd.NaT or (isinstance(value, float) and math.isnan(value)):
        return ""  # NaT first: it is a datetime too, and has no time
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    if isinstance(value, dt.datetime) and value.time() == dt.time():
        return value.date().isoformat()  # pandas' Timestamp is a datetime as well
    return value


def _write_text(frame: "DataFrame", dst: Path, sep: str) -> None:
    rows = frame.astype(object).map(text_cell) if not frame.empty else frame
    with dst.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel reads it as UTF-8
        writer = csv.writer(f, delimiter=sep, lineterminator="\r\n")
        writer.writerows(rows.itertuples(index=False, name=None))


def _write_book(book: dict[str, "DataFrame"], dst: Path, ext: str) -> None:
    import pandas as pd

    with pd.ExcelWriter(dst, engine=_BOOK_ENGINES[ext]) as writer:
        for name, frame in book.items():
            frame.to_excel(writer, sheet_name=sheet_name(name), header=False, index=False)


def _convert(paths: list[Path], ctx: "JobContext", ext: str) -> list[Path]:
    outputs = []
    for src in paths:
        book = read_book(src)
        if ext in _BOOK_ENGINES:
            dst = ctx.output_path(src, ext=ext)
            _write_book(book, dst, ext)
            outputs.append(dst)
            continue
        sep = "\t" if ext == "tsv" else ","
        several = len(book) > 1
        for i, (name, frame) in enumerate(book.items()):
            if ctx.cancelled():
                return outputs
            dst = ctx.output_path(src, ext=ext, label=sheet_name(name) if several else None)
            _write_text(frame, dst, sep)
            outputs.append(dst)
            ctx.progress((i + 1) / len(book), name)
    return outputs


# --- Ops, in default petal order (§6.1) ------------------------------------------------------
@op(id="convert.sheet.xlsx", wheel="convert", label="XLSX", kinds=SHEET, hide_if_same_ext="xlsx")
def to_xlsx(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "xlsx")


@op(id="convert.sheet.csv", wheel="convert", label="CSV", kinds=SHEET, hide_if_same_ext="csv")
def to_csv(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "csv")


@op(id="convert.sheet.tsv", wheel="convert", label="TSV", kinds=SHEET, hide_if_same_ext="tsv")
def to_tsv(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "tsv")


@op(id="convert.sheet.ods", wheel="convert", label="ODS", kinds=SHEET, hide_if_same_ext="ods")
def to_ods(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _convert(paths, ctx, "ods")
