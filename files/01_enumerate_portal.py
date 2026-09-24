"""
STEP 1 — Enumerate the Open Government AIA collection.

Reads every record in the 'aia' collection through the CKAN API, records each
record and each attached file, works out how many distinct AIA submissions each
record contains, and downloads the JSON submissions for later steps.

This is the step that answers "how big is the backlog really?" — a portal record
is a system, but a single record can hold several submissions (ATIP holds at
least four), so the submission count is higher than the record count.

Outputs
    data/og_records.csv    one row per portal record
    data/og_resources.csv  one row per attached file
    raw/submissions/*.json downloaded AIA result files
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
from common import (CKAN_SEARCH, RAW_DIR, fetch, fetch_json, write_csv,
                    absolute_url, split_bilingual, log, header)

# --------------------------------------------------------------------------
# Resource classification
# --------------------------------------------------------------------------
# A record holds more than the assessment itself: peer reviews, data
# dictionaries and guides live alongside it and must not be scored.
NON_ASSESSMENT_HINTS = (
    "peer review", "examen par les pairs", "data dictionary",
    "dictionnaire de donnees", "dictionnaire de données",
    "guide", "annex", "annexe", "executive summary", "resume analytique",
    "résumé analytique", "gba", "acs plus",
)

FRENCH_HINTS = ("french", "francais", "français", "-fr", "_fr", "(fr)", "fr)")
ENGLISH_HINTS = ("english", "anglais", "-en", "_en", "(en)", "en)")

# "2021 Update 2", "2022 Update", "Update 3", "2021 Mise a jour"
UPDATE_RE = re.compile(
    r"((?:19|20)\d{2})?\s*(?:update|mise\s*[àa]\s*jour|revision|r[ée]vision)\s*(\d+)?",
    re.I)


def classify_language(name: str, res: dict) -> str:
    """
    The portal states the language of each file explicitly, so that is used
    first and the file name is only a fallback.

    The field holds ISO codes: ["en"], ["fr"], or both for a bilingual file.
    An earlier version of this only looked for "eng"/"fra" or patterns in the
    file name, so a resource CKAN had already labelled came back "unknown".
    That mattered more than it sounds: the French assessments were being
    reported as missing when the portal knew about them all along, and a
    record whose files were both "unknown" could have its French copy picked
    for English extraction.
    """
    field = res.get("language")
    codes = set()
    if isinstance(field, list):
        codes = {str(c).strip().lower() for c in field if c}
    elif field:
        codes = {c.strip().lower() for c in str(field).replace(",", " ").split()}

    has_en = bool(codes & {"en", "eng", "english", "en-ca"})
    has_fr = bool(codes & {"fr", "fra", "fre", "french", "fr-ca"})
    if has_en and has_fr:
        return "bilingual"
    if has_fr:
        return "fr"
    if has_en:
        return "en"

    # No usable field: fall back to the file name.
    blob = str(name or "").lower()
    name_fr = any(h in blob for h in FRENCH_HINTS)
    name_en = any(h in blob for h in ENGLISH_HINTS)
    if name_fr and name_en:
        return "bilingual"
    if name_fr:
        return "fr"
    if name_en:
        return "en"
    return "unknown"


def is_assessment(name: str, fmt: str) -> bool:
    low = name.lower()
    if any(h in low for h in NON_ASSESSMENT_HINTS):
        return False
    return fmt.upper() in ("PDF", "JSON", "CSV", "XLS", "XLSX", "DOCX")


def submission_label(name: str) -> str:
    """
    Derive a submission label from a resource name.
    'Original' is used where a record shows no update marker, which is the
    common case for records holding a single submission.
    """
    m = UPDATE_RE.search(name)
    if not m:
        return "Original"
    year, seq = m.group(1), m.group(2)
    parts = [p for p in (year, "Update", seq) if p]
    return " ".join(parts)


def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("en") or v.get("fr")
        if v:
            return v
    return default


def main():
    header("STEP 1 — Enumerating the Open Government AIA collection")

    log(f"GET {CKAN_SEARCH}")
    payload = fetch_json(CKAN_SEARCH)
    if not payload.get("success"):
        raise SystemExit("CKAN request did not succeed; inspect the response.")
    result = payload["result"]
    packages = result["results"]
    log(f"  collection reports {result['count']} records; retrieved {len(packages)}")
    if result["count"] > len(packages):
        log("  WARNING: more records exist than were returned. Raise rows= in CKAN_SEARCH.")

    records, resources = [], []
    downloaded = skipped = 0

    for pkg in packages:
        rid = pkg["id"]
        title_en = pick(pkg, "title_translated", "title")
        title_fr = (pkg.get("title_translated") or {}).get("fr", "")
        org = (pkg.get("organization") or {})

        res_rows, labels = [], set()
        for res in pkg.get("resources", []):
            name = pick(res, "name_translated", "name")
            fmt = (res.get("format") or "").upper()
            label = submission_label(name)
            assess = is_assessment(name, fmt)
            if assess:
                labels.add(label)
            res_rows.append({
                "resource_id": res.get("id"),
                "og_record_id": rid,
                "resource_name": name,
                "format": fmt,
                "language": classify_language(name, res),
                "resource_type": pick(res, "resource_type", default=""),
                "submission_label": label,
                "is_assessment_artifact": "Y" if assess else "N",
                "download_url": absolute_url(res.get("url")),
                "size_bytes": res.get("size") or "",
                "last_modified": res.get("last_modified") or res.get("created") or "",
            })

        records.append({
            "og_record_id": rid,
            "title_en": title_en,
            "title_fr": title_fr,
            "department_en": split_bilingual(org.get("title", ""))[0],
            "department_fr": split_bilingual(org.get("title", ""))[1],
            "department_code": org.get("name", ""),
            "record_released": pkg.get("date_released") or pkg.get("metadata_created", "")[:10],
            "record_modified": (pkg.get("metadata_modified") or "")[:10],
            "date_published": pkg.get("date_published", ""),
            "keywords": "; ".join((pkg.get("keywords") or {}).get("en", []))
                        if isinstance(pkg.get("keywords"), dict) else "",
            "homepage": pkg.get("url", ""),
            "portal_url_en": f"https://open.canada.ca/data/en/dataset/{rid}",
            "portal_url_fr": f"https://open.canada.ca/data/fr/dataset/{rid}",
            "resource_count": len(res_rows),
            "submission_count_estimate": len(labels) or 1,
            "has_json": "Y" if any(r["format"] == "JSON" and r["is_assessment_artifact"] == "Y"
                                   for r in res_rows) else "N",
        })
        resources.extend(res_rows)

        # download the JSON submissions
        for r in res_rows:
            if r["format"] != "JSON" or r["is_assessment_artifact"] != "Y":
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{rid}__{r['submission_label']}")[:120]
            dest = RAW_DIR / "submissions" / f"{safe}.json"
            if dest.exists():
                skipped += 1
                continue
            try:
                body = fetch(r["download_url"])
                json.loads(body)                      # validate before writing
                dest.write_text(body, encoding="utf-8")
                downloaded += 1
                log(f"  downloaded {dest.name}")
            except Exception as e:
                log(f"  ! could not download {r['resource_name']}: {e}")

    write_csv("og_records.csv", records)
    write_csv("og_resources.csv", resources)

    # ---------------------------------------------------------------- summary
    header("SUMMARY")
    multi = [r for r in records if r["submission_count_estimate"] > 1]
    total_subs = sum(r["submission_count_estimate"] for r in records)
    log(f"  portal records                : {len(records)}")
    log(f"  estimated submissions         : {total_subs}")
    log(f"  records with >1 submission    : {len(multi)}")
    log(f"  records publishing JSON       : {sum(1 for r in records if r['has_json']=='Y')}")
    log(f"  JSON files downloaded         : {downloaded} (already present: {skipped})")
    if multi:
        log("\n  Records holding several submissions:")
        for r in sorted(multi, key=lambda x: -x["submission_count_estimate"]):
            log(f"    {r['submission_count_estimate']}x  {r['title_en'][:58]}  [{r['department_code']}]")
    log("\n  PDF-only records (need step 6):")
    for r in records:
        if r["has_json"] == "N":
            log(f"    - {r['title_en'][:62]}  [{r['department_code']}]")
    log("\nNext: python 02_parse_catalogs.py")


if __name__ == "__main__":
    main()
