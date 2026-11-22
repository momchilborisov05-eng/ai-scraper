"""CSV, JSON and Excel export. Columns follow the schema order; every function returns bytes so the CLI writes
them to disk and the web app streams them without temporary files."""

from __future__ import annotations

import csv
import io
import json

from .schema import Schema

FORMATS = {"csv": "text/csv", "json": "application/json",
           "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _cell(v) -> object:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return v


def _safe(v):
    """Neutralise spreadsheet formula injection: text starting with = + - @ is prefixed with a quote."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(rows: list[dict], schema: Schema) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(schema.names)
    for r in rows:
        w.writerow([_safe(_cell(r.get(n))) for n in schema.names])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 (£, é) correctly


def to_json(rows: list[dict], schema: Schema) -> bytes:
    data = [{n: r.get(n) for n in schema.names} for r in rows]
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


def to_excel(rows: list[dict], schema: Schema, title: str = "Data") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = (title or "Data")[:31]
    ws.append(schema.names)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([_safe(_cell(r.get(n))) for n in schema.names])
    for i, f in enumerate(schema.fields, 1):
        letter = get_column_letter(i)
        width = max([len(f.name)] + [len(str(_cell(r.get(f.name)))) for r in rows[:500]])
        ws.column_dimensions[letter].width = min(60, width + 2)
        if f.type == "number":
            for cell in ws[letter][1:]:
                cell.number_format = "#,##0.00"
        elif f.type == "integer":
            for cell in ws[letter][1:]:
                cell.number_format = "#,##0"
        elif f.type == "url":
            for cell in ws[letter][1:]:
                if isinstance(cell.value, str) and cell.value.startswith(("http://", "https://")):
                    cell.hyperlink = cell.value
                    cell.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def export(rows: list[dict], schema: Schema, fmt: str) -> bytes:
    if fmt == "csv":
        return to_csv(rows, schema)
    if fmt == "json":
        return to_json(rows, schema)
    if fmt == "xlsx":
        return to_excel(rows, schema)
    raise ValueError(f"unknown format {fmt!r}; use csv, json or xlsx")
