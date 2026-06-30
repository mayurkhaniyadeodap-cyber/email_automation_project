"""Export report rows (list of dicts) to CSV / Excel (.xlsx) / PDF. Returns an HttpResponse."""

import csv
import io

from django.http import HttpResponse


def _columns(rows):
    return list(rows[0].keys()) if rows else []


def to_csv(rows, filename="report"):
    cols = _columns(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return resp


def to_excel(rows, filename="report"):
    from openpyxl import Workbook

    cols = _columns(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(cols)
    for r in rows:
        ws.append([_s(r.get(c)) for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return resp


def to_pdf(rows, filename="report", title="Report"):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet

    cols = _columns(rows)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=12 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    data = [cols] + [[_s(r.get(c))[:40] for c in cols] for r in rows[:1000]]
    elems = [Paragraph(title, styles["Heading2"])]
    if data and cols:
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        elems.append(table)
    else:
        elems.append(Paragraph("No data.", styles["Normal"]))
    doc.build(elems)
    resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return resp


def _s(v):
    return "" if v is None else str(v)


def export(fmt, rows, filename="report", title="Report"):
    fmt = (fmt or "csv").lower()
    if fmt in ("xlsx", "excel"):
        return to_excel(rows, filename)
    if fmt == "pdf":
        return to_pdf(rows, filename, title)
    return to_csv(rows, filename)
