"""
STEP 4 — Ingest the published JSON submissions.

Reads every AIA result file downloaded in step 1, scores it against the catalog
built in step 2, and produces the three assessment tables. A portal record is a
system; each result file inside it is a submission, numbered in date order.

Scores are recomputed from the answers rather than copied from a summary, so any
disagreement with the published figure is visible instead of assumed away.

Free-text answers keep their language. The tool stores English answers in 'data'
and any French the department supplied in 'translationsOnResult', so both are
carried through with the source recorded rather than machine-translated.

Outputs
    data/systems.csv
    data/submissions.csv
    data/answers.csv
    data/ingest_issues.csv
"""
from __future__ import annotations
import json, re
from collections import defaultdict
from pathlib import Path
from common import (RAW_DIR, read_csv, write_csv, answer_points, option_points,
                    impact_level, current_score, version_key, split_bilingual,
                    load_option_lookup, log, header)

SKIP_KEYS = {"currentPage", "version", "pageNo"}


def load_catalog():
    fields = {}
    for r in read_csv("question_map.csv"):
        fields[(r["catalog_version"], r["field_name"])] = r
    # Reassembled from the normalised catalog tables and keyed by the value that
    # appears in a saved file, which is how an answer is matched to its choice.
    options = defaultdict(dict)
    for key, opts in load_option_lookup().items():
        for o in opts:
            options[key][o["option_value"]] = o
    versions = {r["catalog_version"]: r for r in read_csv("catalog_versions.csv")}
    return fields, options, versions


def nearest_version(v, known):
    """Fall back to the closest catalog we hold if a file names a version we lack."""
    if v in known:
        return v, "exact"
    if not known:
        return None, "none"
    # Compare component by component rather than by the sum of components:
    # summing treats 0.10.0 and 0.1.9 as equally close to 0.8, which they are not.
    # On a tie, prefer the earlier catalog — a submission cannot have been
    # answered against a version that did not exist yet.
    target = version_key(v)
    def distance(k):
        a = version_key(k)
        return (tuple(abs(x - y) for x, y in zip(a, target)), a > target, a)
    best = min(known, key=distance)
    return best, "nearest"


def main():
    header("STEP 4 — Ingesting published JSON submissions")

    records = {r["og_record_id"]: r for r in read_csv("og_records.csv")}
    resources = read_csv("og_resources.csv")
    fields, options, versions = load_catalog()
    known = set(versions)
    log(f"  catalog versions available: {', '.join(sorted(known, key=version_key))}")

    res_by_record = defaultdict(list)
    for r in resources:
        res_by_record[r["og_record_id"]].append(r)

    files = sorted((RAW_DIR / "submissions").glob("*.json"))
    log(f"  submission files to read : {len(files)}\n")

    parsed, issues = [], []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            issues.append({"file": path.name, "issue": f"unreadable: {e}"})
            continue

        rid = path.name.split("__")[0]
        label = path.stem.split("__")[-1].replace("_", " ")
        data = doc.get("data", doc)
        stated = str(doc.get("version", "")).lstrip("v")
        cat, how = nearest_version(stated, known)
        if not cat:
            issues.append({"file": path.name, "issue": "no catalog available"})
            continue
        if how != "exact":
            issues.append({"file": path.name,
                           "issue": f"file states v{stated}; scored against v{cat}"})

        translations = doc.get("translationsOnResult", {}) or {}
        raw = mit = 0
        rows, unmapped = [], 0

        for key, val in data.items():
            if key in SKIP_KEYS:
                continue
            fld = fields.get((cat, key))
            if fld is None:
                unmapped += 1
                pts = answer_points(val)
                rows.append({
                    "field_name": key, "question_uid": "", "point_type": "unknown",
                    "points": pts, "answer_value_raw": json.dumps(val, ensure_ascii=False)
                             if isinstance(val, list) else val,
                    "option_index": "", "answer_text_en": "", "answer_text_fr": "",
                    "is_free_text": "N", "mapped": "N",
                })
                continue

            ptype = fld["point_type"]
            pts = answer_points(val)
            if ptype == "raw":
                raw += pts
            elif ptype == "mitigation":
                mit += pts

            opts = options.get((cat, key), {})
            vals = val if isinstance(val, list) else [val]
            idx = ";".join(str(opts[v]["option_index"]) for v in vals if v in opts)
            en = ";".join(opts[v]["text_en"] for v in vals if v in opts)
            fr = ";".join(opts[v]["text_fr"] for v in vals if v in opts)
            free = not opts and isinstance(val, str)

            rows.append({
                "field_name": key,
                "question_uid": fld["question_uid"],
                "point_type": ptype,
                "points": pts,
                "answer_value_raw": json.dumps(val, ensure_ascii=False)
                                    if isinstance(val, list) else val,
                "option_index": idx,
                "answer_text_en": val if free else en,
                "answer_text_fr": translations.get(key, "") if free else fr,
                "is_free_text": "Y" if free else "N",
                "translation_source": ("submitted_translation" if free and key in translations
                                       else "catalog" if not free else "missing"),
                "mapped": "Y",
            })

        vinfo = versions[cat]
        max_mit = float(vinfo["max_mitigation"] or 0)
        max_raw = float(vinfo["max_raw"] or 0)
        cur, reduced = current_score(raw, mit, max_mit)
        pct = round(cur / max_raw * 100, 1) if max_raw else None

        parsed.append({
            "og_record_id": rid, "file": path.name, "submission_label": label,
            "catalog_version": cat, "stated_version": stated, "version_source": "json_stamped",
            "raw_impact_score": raw, "mitigation_score": mit, "current_score": cur,
            "reduction_applied": "Y" if reduced else "N",
            "current_score_pct": pct,
            "mitigation_pct": round(mit / max_mit * 100, 1) if max_mit else None,
            "impact_level": impact_level(pct),
            "answer_count": len(rows), "unmapped_answers": unmapped,
            "rows": rows,
            "title": (data.get("projectDetailsTitle") or records.get(rid, {}).get("title_en", "")),
            "title_fr": translations.get("projectDetailsTitle", ""),
            "department_code": data.get("projectDetailsDepartment-NS", ""),
            "branch": data.get("projectDetailsBranch", ""),
            "phase": data.get("projectDetailsPhase", ""),
            "aia_number_selfreported": data.get("projectAIAnumber", ""),
        })
        if unmapped:
            issues.append({"file": path.name,
                           "issue": f"{unmapped} answers not found in the v{cat} catalog"})

    # ------------------------------------------------------------------ ids
    by_record = defaultdict(list)
    for p in parsed:
        by_record[p["og_record_id"]].append(p)

    def sort_key(p):
        m = re.search(r"(19|20)\d{2}", p["submission_label"])
        return (m.group(0) if m else "0000", p["submission_label"])

    # The Open Government record identifier is the key. It is one-to-one with a
    # system by definition — a record is a system — and it never moves. The
    # short number kept alongside it is assigned by row order, so it shifts
    # whenever a record is added or withdrawn; it is for reading, not joining.
    systems, submissions, answers = [], [], []
    for ordinal, (rid, group) in enumerate(sorted(by_record.items()), start=1):
        group.sort(key=sort_key)
        rec = records.get(rid, {})
        latest = group[-1]
        for seq, p in enumerate(group, start=1):
            sub_id = f"{rid}-{seq}"
            submissions.append({
                "submission_id": sub_id, "og_record_id": rid, "sequence_no": seq,
                "system_ref": ordinal, "submission_label": p["submission_label"],
                "catalog_version": p["catalog_version"],
                "stated_version": p["stated_version"],
                "version_source": p["version_source"],
                "publication_date": rec.get("record_released", ""),
                "raw_impact_score": p["raw_impact_score"],
                "mitigation_score": p["mitigation_score"],
                "current_score": p["current_score"],
                "reduction_applied": p["reduction_applied"],
                "current_score_pct": p["current_score_pct"],
                "mitigation_pct": p["mitigation_pct"],
                "impact_level": p["impact_level"],
                "answer_count": p["answer_count"],
                "unmapped_answers": p["unmapped_answers"],
                "source_format": "JSON",
                "source_file": p["file"],
                "extraction_method": "native_json",
                "is_current": "Y" if p is latest else "N",
            })
            for r in p["rows"]:
                answers.append({"submission_id": sub_id, "og_record_id": rid,
                                "catalog_version": p["catalog_version"], **r})

        systems.append({
            "og_record_id": rid, "system_ref": ordinal,
            "system_name_en": latest["title"] or rec.get("title_en", ""),
            "system_name_fr": latest["title_fr"] or rec.get("title_fr", ""),
            "department_en": rec.get("department_en", ""),
            "department_fr": rec.get("department_fr", ""),
            "department_code": latest["department_code"] or rec.get("department_code", ""),
            "portal_department_code": rec.get("department_code", ""),
            "branch": latest["branch"],
            "submission_count": len(group),
            "first_submission_label": group[0]["submission_label"],
            "latest_submission_label": latest["submission_label"],
            "current_submission_id": f"{rid}-{len(group)}",
            "current_impact_level": latest["impact_level"],
            "current_score_pct": latest["current_score_pct"],
            "portal_url_en": rec.get("portal_url_en", ""),
        })

    write_csv("systems.csv", systems)
    write_csv("submissions.csv", submissions)
    write_csv("answers.csv", answers)
    write_csv("ingest_issues.csv", issues)

    header("SUMMARY")
    log(f"  systems      : {len(systems)}")
    log(f"  submissions  : {len(submissions)}")
    log(f"  answers      : {len(answers)}")
    log(f"  issues       : {len(issues)}  -> ingest_issues.csv")
    multi = [s for s in systems if s["submission_count"] > 1]
    log(f"\n  systems with more than one submission: {len(multi)}")
    for s in sorted(multi, key=lambda x: -x["submission_count"]):
        log(f"    {s['submission_count']}x  {s['system_name_en'][:56]}")
    unmapped = sum(int(s["unmapped_answers"]) for s in submissions)
    if unmapped:
        log(f"\n  answers with no catalog match: {unmapped}")
        log("  A high count means step 2 is missing the version those files were answered under.")
    log("\nNext: python 05_build_workbook.py")


if __name__ == "__main__":
    main()
