"""
STEP 5 — Assemble the master workbook.

Collects every CSV produced by the earlier steps into one formatted xlsx, ready
for Power BI. Values only, no formulas and no Excel table objects, matching the
convention the existing reporting model expects.

Outputs
    aia_master_table.xlsx
"""
from __future__ import annotations
from pathlib import Path
import csv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from common import DATA_DIR, BASE, read_csv, log, header, assemble_core_tables

OUT = BASE / "aia_master_table.xlsx"

TEAL, PALE, GOLD, RED, GREEN = "2C5F5A", "EAF2F1", "F5EFE0", "FBEAEA", "E4F1E6"
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# sheet name -> (csv file, note for the contents page)
SHEETS = [
    ("systems",           "systems.csv",           "One row per system. A system is one Open Government record."),
    ("submissions",       "submissions.csv",       "One row per completed AIA. A system may have several."),
    ("answers",           "answers.csv",           "One row per question per submission."),
    ("questions",         "questions.csv",         "DERIVED roll-up of question_map, one row per question. Convenience dimension; question_map is the source."),
    ("question_map",      "question_map.csv",      "One row per question per version: wording, guidance, branching, points, parent, and its question_uid."),
    ("question_options",  "question_options.csv",  "One row per question: which label set and scoring pattern it uses."),
    ("option_sets",       "option_sets.csv",       "Distinct sets of answer labels, stored once."),
    ("option_set_items",  "option_set_items.csv",  "The labels within each set, in both languages."),
    ("option_points",     "option_point_profiles.csv", "Distinct scoring patterns across the label sets."),
    ("option_point_items","option_point_profile_items.csv", "Points for each choice within a scoring pattern."),
    ("departments",       "departments.csv",       "Government organisations, bilingual. Reference data, not an answer set."),
    ("sections",          "sections.csv",          "Distinct sections, bridged across versions."),
    ("section_versions",  "section_versions.csv",  "Section as it appears in each version, with maxima."),
    ("catalog_versions",  "catalog_versions.csv",  "Maximum attainable scores per AIA version."),
    ("og_records",        "og_records.csv",        "Open Government records as published."),
    ("og_resources",      "og_resources.csv",      "Every file attached to each record."),
    ("bridge_review",     "bridge_review.csv",     "Cross-version matches below the confidence threshold."),
    ("ingest_issues",     "ingest_issues.csv",     "Anything that did not parse cleanly."),
    ("pdf_triage",        "pdf_triage.csv",        "Which questionnaire version each PDF-only record used."),
    ("pdf_validation",    "pdf_submissions.csv",   "PDF extractions checked against the scores printed in each document."),
    ("pdf_extract_review","pdf_extract_review.csv","PDF extractions that failed validation and were NOT merged."),
    ("services",          "services.csv",          "GC Service Inventory services that declare an automated decision system."),
    ("system_services",   "system_services.csv",   "Confirmed links between an assessed system and a service, with confidence and rationale."),
    ("crosswalk_gaps",    "crosswalk_gaps.csv",    "Assessments with no service named, and services declaring automation with no assessment."),
]

NUMERIC = {"points", "max_points", "option_set_id", "point_profile_id", "chain_depth",
           "service_count", "system_ref",
           "parent_question_uid", "root_question_uid", "raw_impact_score", "mitigation_score",
           "current_score", "current_score_pct", "mitigation_pct", "impact_level",
           "max_raw", "max_mitigation", "mitigation_threshold", "answer_count",
           "unmapped_answers", "submission_count", "sequence_no",
           "question_uid", "section_uid", "option_index", "option_count",
           "max_raw_points", "max_mitigation_points", "match_score",
           "display_order", "resource_count", "submission_count_estimate",
           "question_count", "scored_question_count", "section_count",
           "version_count", "field_id", "option_id", "points_added"}

WIDE = {"service_names_en", "service_description_en", "automation_description_en",
        "match_rationale", "reason", "service_uri_en", "program_name_en",
        "labels", "points_pattern", "visible_if", "department_en", "department_fr",
        "name_en", "name_fr", "canonical_text_en", "canonical_text_fr", "text_en", "text_fr",
        "guidance_en", "guidance_fr", "answer_text_en", "answer_text_fr",
        "system_name_en", "system_name_fr", "title_en", "title_fr",
        "resource_name", "download_url", "portal_url_en", "portal_url_fr",
        "note", "issue", "field_names", "keywords", "answer_value_raw"}


def typed(key, val):
    if val in (None, ""):
        return None
    if key in NUMERIC:
        try:
            f = float(val)
            return int(f) if f.is_integer() else f
        except ValueError:
            return val
    return val


def add_sheet(wb, name, rows, note=""):
    ws = wb.create_sheet(name[:31])
    if not rows:
        ws["A1"] = f"(no rows — {note})"
        ws["A1"].font = Font(name="Arial", size=10, italic=True, color="808080")
        return ws, 0
    headers = list(rows[0].keys())
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=TEAL)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = BORDER
    for r, row in enumerate(rows, 2):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(r, c, typed(h, row.get(h)))
            cell.font = Font(name="Arial", size=10)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center")
            if h == "needs_review" and row.get(h) == "Y":
                cell.fill = PatternFill("solid", fgColor=GOLD)
            elif h == "is_current" and row.get(h) == "Y":
                cell.fill = PatternFill("solid", fgColor=GREEN)
            elif h == "point_type" and row.get(h) == "unknown":
                cell.fill = PatternFill("solid", fgColor=RED)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30
    for c, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(c)].width = 52 if h in WIDE else min(max(11, len(h) + 4), 30)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
    return ws, len(rows)


def main():
    header("STEP 5 — Building the master workbook")
    wb = Workbook()
    wb.remove(wb.active)
    contents = wb.create_sheet("contents")

    # PDF assessments are merged into the core tables before anything is written,
    # so run this step after 07_extract_pdf_answers.py.
    def rd(name):
        p = DATA_DIR / name
        if not p.exists():
            return []
        with open(p, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    # The same assembly step 10 publishes from, so the workbook and the
    # published tables always hold the same systems.
    merged = {f"{k}.csv": v for k, v in assemble_core_tables().items()}

    manifest = []
    for sheet, csvname, note in SHEETS:
        rows = merged.get(csvname) if csvname in merged else rd(csvname)
        if not rows:
            log(f"  - {csvname} not found; skipped")
            manifest.append({"sheet": sheet, "rows": "not built", "contents": note})
            continue
        _, n = add_sheet(wb, sheet, rows, note)
        log(f"  {sheet:<20} {n:>7} rows")
        manifest.append({"sheet": sheet, "rows": n, "contents": note})

    # contents page
    contents["A1"] = "AIA Master Table"
    contents["A1"].font = Font(name="Arial", size=16, bold=True, color="1B3A36")
    contents["A2"] = ("Built directly from the Open Government AIA collection and the "
                      "published questionnaire. Systems and submissions are separate: a "
                      "record is a system, and a system may hold several submissions.")
    contents["A2"].font = Font(name="Arial", size=10, italic=True, color="5A5A5A")
    contents["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    contents.merge_cells("A2:C2")
    contents.row_dimensions[2].height = 42

    for c, h in enumerate(["sheet", "rows", "contents"], 1):
        cell = contents.cell(4, c, h)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=TEAL)
        cell.border = BORDER
    for r, m in enumerate(manifest, 5):
        for c, h in enumerate(["sheet", "rows", "contents"], 1):
            cell = contents.cell(r, c, m[h])
            cell.font = Font(name="Arial", size=10)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=(h == "contents"))
    contents.column_dimensions["A"].width = 22
    contents.column_dimensions["B"].width = 12
    contents.column_dimensions["C"].width = 82

    notes_row = len(manifest) + 7
    for i, line in enumerate([
        "Reading notes",
        "Raw scores are not comparable across AIA versions. Use current_score_pct.",
        "A count of submissions is not a count of systems currently operating; an "
        "assessment records a system at one moment in its life.",
        "Rows flagged needs_review in question_map were matched across versions with "
        "low confidence. Exclude them from cross-version comparisons until checked.",
        "point_type 'unknown' means the panel suffix was not recognised in step 2.",
    ]):
        cell = contents.cell(notes_row + i, 1, line)
        cell.font = Font(name="Arial", size=10, bold=(i == 0),
                         color="1B3A36" if i == 0 else "5A5A5A")
        contents.merge_cells(start_row=notes_row + i, start_column=1,
                             end_row=notes_row + i, end_column=3)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    wb.move_sheet("contents", offset=-len(wb.sheetnames) + 1)
    wb.save(OUT)
    log(f"\n  saved {OUT}")


if __name__ == "__main__":
    main()
