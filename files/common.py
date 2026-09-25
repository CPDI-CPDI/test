"""
Shared configuration and helpers for the AIA master table pipeline.

Every script writes CSVs into DATA_DIR. Nothing overwrites a source file;
downloaded artefacts land in RAW_DIR and are never modified.
"""
from __future__ import annotations
import csv, json, os, re, sys, time, unicodedata, difflib
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------- paths
# Works whether the scripts sit in a 'scripts/' subfolder or flat in the
# working directory. Without this check, running them flat puts every output
# one level above where the person is working.
_HERE = Path(__file__).resolve().parent
BASE = _HERE.parent if _HERE.name.lower() == "scripts" else _HERE


def find_repo_root(start: Path) -> Path | None:
    """The nearest folder at or above `start` that holds a .git, if any."""
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return p
    return None


# The published tables belong at the root of the repository, because the raw
# URLs a report points at are built from that path. The scripts live in a
# subfolder of the repository (files/), so writing beside them put the tables
# one level too deep. Outside a repository — a plain working folder — they
# still land beside the scripts.
PUBLISH_DIR = (find_repo_root(BASE) or BASE) / "published"

DATA_DIR = BASE / "data"          # tidy CSV output
RAW_DIR  = BASE / "raw"           # downloaded JSON / PDF, untouched
LOG_DIR  = BASE / "logs"
for d in (DATA_DIR, RAW_DIR, LOG_DIR, RAW_DIR / "submissions",
          RAW_DIR / "catalogs", RAW_DIR / "pdf"):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- endpoints
CKAN_SEARCH = ("https://open.canada.ca/data/api/action/package_search"
               "?fq=collection:aia&rows=200")
GITHUB_TAGS = "https://api.github.com/repos/canada-ca/aia-eia-js/tags"
SURVEY_RAW  = ("https://raw.githubusercontent.com/canada-ca/aia-eia-js/"
               "{ref}/src/survey-enfr.json")

USER_AGENT = "AIA-master-table-builder/1.0 (Government of Canada internal analysis)"

# ---------------------------------------------------------------- scoring
# Impact level thresholds, as a percentage of maximum attainable raw score.
# Source: Directive on Automated Decision-Making, Appendix C.
IMPACT_LEVELS = [(1, 0, 25), (2, 26, 50), (3, 51, 75), (4, 76, 100)]

# If mitigation reaches this share of the maximum attainable mitigation score,
# the raw impact score is reduced by MITIGATION_REDUCTION.
MITIGATION_THRESHOLD = 0.80
MITIGATION_REDUCTION = 0.15

# Panel-name suffixes observed in survey-enfr.json.
#   -RS  risk scored      -> counts toward the raw impact score
#   -NS  not scored       -> recorded but carries no points
# Mitigation panels are identified by page name (see MITIGATION_PAGE_HINTS),
# because the suffix convention does not distinguish them.
# 02_parse_catalogs.py prints every suffix it finds; if a new one appears,
# add it here rather than letting it fall through to "unknown".
PANEL_SUFFIX_POINT_TYPE = {
    "RS": "raw",
    "NS": "none",
    "MS": "mitigation",
}

# Page-name fragments that mark a page as mitigation rather than raw impact.
MITIGATION_PAGE_HINTS = (
    "consultation", "dataquality", "fairness", "privacy",
    "derisking", "mitigation", "procedural",
)

# Mitigation questions come in paired Design and Implementation pages. A given
# assessment answers one set or the other depending on the project's phase, so
# the maximum attainable mitigation score is the larger of the two, never their
# sum. Summing them overstates the maximum by roughly a factor of two, which
# then halves every mitigation percentage and suppresses the 15% reduction.
DESIGN_PAGE_HINT = "design"
IMPLEMENTATION_PAGE_HINT = "implementation"


def mitigation_phase(page_name: str) -> str:
    """Return 'design', 'implementation' or 'shared' for a mitigation page."""
    low = (page_name or "").lower()
    if DESIGN_PAGE_HINT in low:
        return "design"
    if IMPLEMENTATION_PAGE_HINT in low:
        return "implementation"
    return "shared"


def split_bilingual(value: str):
    """
    The portal stores an organisation as one string with both official
    languages joined by a pipe: "Transport Canada | Transports Canada".
    Returns (english, french); where no separator is present the same value is
    returned for both, since one language is better than none.
    """
    text = (value or "").strip()
    if not text:
        return "", ""
    for sep in (" | ", "|", " / "):
        if sep in text:
            en, _, fr = text.partition(sep)
            return en.strip(), fr.strip()
    return text, text


def clean_version(s: str) -> str:
    """
    Repository tags are inconsistent: 'v0.10.0', 'v.0.8a1', 'v.0.6'.
    Normalise to the bare version so joins between tables line up.
    """
    return str(s).strip().lstrip("vV").lstrip(".").strip()


def absolute_url(url: str, base: str = "https://open.canada.ca") -> str:
    """CKAN returns some resource URLs as site-relative paths."""
    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return base.rstrip("/") + u
    if not u.lower().startswith(("http://", "https://")):
        return base.rstrip("/") + "/" + u
    return u

# ---------------------------------------------------------------- http
def fetch(url: str, *, binary: bool = False, retries: int = 3, pause: float = 1.5):
    """GET a URL with retries. Returns str (or bytes when binary=True)."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=60) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                log(f"  retry {attempt}/{retries} after error: {e}")
                time.sleep(pause * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last}")


def fetch_json(url: str):
    return json.loads(fetch(url))

# ---------------------------------------------------------------- text
def norm(s) -> str:
    """Normalise text for comparison: strip accents, punctuation, case, digits."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"section\s*\d+\s*[:.-]?", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def bilingual(node, lang="en"):
    """survey-enfr.json stores English under 'default' and French under 'fr'."""
    if node is None:
        return None
    if isinstance(node, str):
        return node if lang == "en" else None
    if isinstance(node, dict):
        return node.get("default") if lang == "en" else node.get("fr")
    return str(node)

# ---------------------------------------------------------------- points
POINT_RE = re.compile(r"^item\d+-(\d+)$")


def option_points(value) -> int:
    """
    The current AIA encodes points inside the answer value: 'item1-4' is worth 4.
    Returns 0 for values with no encoded score (e.g. 'item1').
    """
    m = POINT_RE.match(str(value).strip())
    return int(m.group(1)) if m else 0


def answer_points(value) -> int:
    """Sum encoded points across a single answer or a checkbox list."""
    vals = value if isinstance(value, list) else [value]
    return sum(option_points(v) for v in vals)


def version_key(v: str):
    """
    Order version strings numerically: '0.9.1' comes before '0.10.0', which
    plain string sorting gets wrong.

    Parts may carry a suffix — the repository tags an early release 'v.0.8a1'.
    Only the leading digits are significant, so '8a1' reads as 8. Without this,
    int('8a1') fails, the part collapses to 0, and v0.8a1 sorts as though it
    were version 0.0.0 — which makes it lose nearest-version matching to
    unrelated releases.
    """
    parts = []
    for p in str(v).split("."):
        m = re.match(r"(\d+)", p.strip())
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def impact_level(pct: float | None):
    if pct is None:
        return None
    for lvl, lo, hi in IMPACT_LEVELS:
        if lo <= pct <= hi:
            return lvl
    return 4 if pct > 100 else 1


def current_score(raw: float, mitigation: float, max_mitigation: float | None):
    """
    Directive rule: if the mitigation score reaches 80% of the maximum attainable
    mitigation score, 15% is deducted from the raw impact score.
    Returns (current_score, reduction_applied).
    """
    if not max_mitigation:
        return raw, None
    if mitigation >= MITIGATION_THRESHOLD * max_mitigation:
        return round(raw * (1 - MITIGATION_REDUCTION)), True
    return raw, False

# ---------------------------------------------------------------- csv
def write_csv(name: str, rows: list[dict], fieldnames: list[str] | None = None):
    path = DATA_DIR / name
    if not rows and not fieldnames:
        # Write nothing but leave no stale file behind. Skipping the write let a
        # file from a previous run survive, so a review list that should have
        # been empty still showed the earlier failures.
        if path.exists():
            path.unlink()
            log(f"  removed stale {name} (nothing to report)")
        return path
    if fieldnames is None:
        seen, fieldnames = set(), []
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k); fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"  wrote {name}  ({len(rows)} rows)")
    return path


def read_csv(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        raise SystemExit(f"missing {path}. Run the earlier pipeline step first.")
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

# ---------------------------------------------------------------- logging
def load_option_lookup():
    """
    Rebuild the per-option view from the normalised catalog tables.

    The answer choices are stored split apart — labels in one table, scoring
    patterns in another, and one row per question linking the two — because the
    same handful of label sets and patterns are reused across hundreds of
    questions. Steps that score or render a questionnaire still want the plain
    list of choices for a question, so it is reassembled here rather than kept
    as a second copy on disk.

    Returns {(catalog_version, field_name): [ {option_value, option_index,
    points, text_en, text_fr}, ... ]} ordered by option_index.
    """
    labels = defaultdict(dict)          # set_id -> index -> {en, fr}
    for r in read_csv("option_set_items.csv"):
        labels[str(r["option_set_id"])][int(r["option_index"])] = {
            "text_en": r.get("text_en", ""), "text_fr": r.get("text_fr", "")}

    points = defaultdict(dict)          # profile_id -> index -> {value, points}
    for r in read_csv("option_point_profile_items.csv"):
        points[str(r["point_profile_id"])][int(r["option_index"])] = {
            "option_value": r.get("option_value", ""),
            "points": int(float(r.get("points") or 0))}

    departments = read_csv("departments.csv")

    out = defaultdict(list)
    for link in read_csv("question_options.csv"):
        key = (str(link["catalog_version"]), link["field_name"])
        set_id = str(link.get("option_set_id", ""))

        if set_id == "DEPARTMENTS":
            for i, d in enumerate(departments):
                out[key].append({
                    "option_value": d["department_code"], "option_index": i,
                    "points": 0, "text_en": d.get("name_en", ""),
                    "text_fr": d.get("name_fr", "")})
            continue

        prof = points.get(str(link.get("point_profile_id", "")), {})
        for idx, lab in sorted(labels.get(set_id, {}).items()):
            p = prof.get(idx, {})
            out[key].append({
                "option_value": p.get("option_value", ""), "option_index": idx,
                "points": p.get("points", 0),
                "text_en": lab["text_en"], "text_fr": lab["text_fr"]})
    return out


# ---------------------------------------------------------------- assembly
# The core tables — systems, submissions, answers — are assembled from three
# sources: the JSON submissions (step 4), the validated PDF extractions
# (step 7) and the service crosswalk (step 9). Both the workbook (step 5) and
# the published files (step 10) read them through assemble_core_tables(), so
# the two can never disagree about what the portfolio contains. Before this,
# the merge lived in step 5 alone and the published tables silently left out
# every PDF-only system.

def _read_optional(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def merge_pdf_results(systems, submissions, answers, og_records=None):
    """
    Fold the validated PDF assessments into the same tables as the JSON ones.

    They belong in `systems`, `submissions` and `answers` rather than in
    separate sheets: a report should not have to union two tables to count the
    portfolio. `source_format` distinguishes them where that matters.

    Only rows whose recomputed scores reproduced the scores printed in the
    document are merged. Anything that failed validation stays out.

    Names and departments come from the portal record, in both languages, since
    the PDF itself is read in English only.
    """
    all_pdf = _read_optional("pdf_submissions.csv")
    if not all_pdf:
        log("  (no pdf_submissions.csv — run 07_extract_pdf_answers.py to include "
            "the PDF-only assessments)")
        return systems, submissions, answers, 0

    pdf_subs = [r for r in all_pdf if r.get("validated") == "Y"]
    pdf_ans = [r for r in _read_optional("pdf_answers.csv") if r.get("validated") == "Y"]
    skipped = len(all_pdf) - len(pdf_subs)
    if skipped:
        log(f"  {skipped} PDF assessment(s) failed validation and were not merged")
    if not pdf_subs:
        return systems, submissions, answers, 0

    if og_records is None:
        og_records = _read_optional("og_records.csv")
    record = {r.get("og_record_id", ""): r for r in og_records}

    existing = {s["og_record_id"] for s in systems}
    next_ref = max((int(s.get("system_ref") or 0) for s in systems
                    if str(s.get("system_ref", "")).isdigit()), default=0) + 1

    by_record = defaultdict(list)
    for r in pdf_subs:
        by_record[r["og_record_id"]].append(r)

    ans_by_key = defaultdict(list)
    for a in pdf_ans:
        ans_by_key[(a["og_record_id"], a["submission_label"])].append(a)

    added = 0
    for rid, group in sorted(by_record.items()):
        if rid in existing:
            log(f"  (record {rid} already present from JSON — PDF copy skipped)")
            continue
        ref = next_ref
        next_ref += 1
        group.sort(key=lambda r: r.get("submission_label", ""))
        latest = group[-1]
        rec = record.get(rid, {})

        for seq, r in enumerate(group, start=1):
            sub_id = f"{rid}-{seq}"
            submissions.append({
                "submission_id": sub_id, "og_record_id": rid, "sequence_no": seq,
                "system_ref": ref, "submission_label": r.get("submission_label", ""),
                "catalog_version": r.get("catalog_version", ""),
                "stated_version": r.get("catalog_version", ""),
                "version_source": r.get("version_source", ""),
                "publication_date": r.get("publication_date", ""),
                "raw_impact_score": r.get("computed_raw"),
                "mitigation_score": r.get("computed_mitigation"),
                "current_score": r.get("computed_current"),
                "reduction_applied": r.get("reduction_applied", ""),
                "current_score_pct": r.get("current_score_pct"),
                "mitigation_pct": "",
                "impact_level": r.get("computed_impact_level"),
                "answer_count": r.get("answer_count"),
                "unmapped_answers": (int(r.get("answer_count") or 0)
                                     - int(r.get("linked_to_catalog") or 0)),
                "source_format": "PDF",
                "source_file": r.get("source_file", ""),
                "extraction_method": r.get("extraction_method", "pdf_text"),
                "is_current": "Y" if r is latest else "N",
            })
            for a in ans_by_key.get((rid, r.get("submission_label", "")), []):
                answers.append({
                    "submission_id": sub_id, "og_record_id": rid,
                    "catalog_version": a.get("catalog_version", ""),
                    "field_name": a.get("field_name", ""),
                    "question_uid": a.get("question_uid", ""),
                    "point_type": a.get("point_type", ""),
                    "points": a.get("points"),
                    "answer_value_raw": "",
                    "option_index": "",
                    "answer_text_en": a.get("answer_text", ""),
                    "answer_text_fr": "",
                    "translation_source": "french_pdf_not_yet_extracted",
                    "is_free_text": a.get("is_free_text", ""),
                    "mapped": "Y" if a.get("question_uid") else "N",
                    "shown": a.get("shown", ""),
                })
            added += 1

        portal_code = rec.get("department_code", "")
        systems.append({
            "og_record_id": rid, "system_ref": ref,
            "system_name_en": rec.get("title_en") or latest.get("title", ""),
            "system_name_fr": rec.get("title_fr", ""),
            "department_en": rec.get("department_en") or latest.get("department", ""),
            "department_fr": rec.get("department_fr", ""),
            "department_code": portal_code,
            "portal_department_code": portal_code,
            "branch": "",
            "submission_count": len(group),
            "first_submission_label": group[0].get("submission_label", ""),
            "latest_submission_label": latest.get("submission_label", ""),
            "current_submission_id": f"{rid}-{len(group)}",
            "current_impact_level": latest.get("computed_impact_level"),
            "current_score_pct": latest.get("current_score_pct"),
            "portal_url_en": f"https://open.canada.ca/data/en/dataset/{rid}",
        })

    log(f"  merged {added} validated PDF assessment(s) into systems/submissions/answers")
    return systems, submissions, answers, added


def merge_crosswalk(systems):
    """
    Attach the service links to the systems table.

    The crosswalk is joined on the Open Government record identifier, which
    does not move, so a crosswalk prepared against an earlier run still lines
    up.

    The gaps are also re-resolved here, because systems added by the PDF merge
    are not present when the crosswalk step runs. Rewriting system_services.csv
    and crosswalk_gaps.csv is idempotent, so calling this from more than one
    step is safe.
    """
    links = _read_optional("system_services.csv")
    if not links:
        return systems, [], 0

    by_record = defaultdict(list)
    for l in links:
        by_record[str(l.get("og_record_id", "")).strip()].append(l)

    ref_of = {str(s.get("og_record_id", "")).strip(): s.get("system_ref") for s in systems}

    matched = 0
    for s in systems:
        rec = str(s.get("og_record_id", "")).strip()
        mine = by_record.get(rec, [])
        if mine:
            matched += 1
        s["service_count"] = len(mine)
        s["service_ids"] = "; ".join(l.get("service_id", "") for l in mine)
        s["service_names_en"] = "; ".join(l.get("service_name_en", "") for l in mine)
        s["service_match_confidence"] = "; ".join(
            sorted({l.get("match_confidence", "") for l in mine if l.get("match_confidence")}))
        s["crosswalk_status"] = ("linked to a service" if mine else "no service named")

    # stamp the current display number onto each link, and re-derive the gaps
    for l in links:
        l["system_ref"] = ref_of.get(str(l.get("og_record_id", "")).strip(), "")

    gaps = [g for g in _read_optional("crosswalk_gaps.csv")
            if g.get("gap_type") != "assessment not yet reviewed"]
    reviewed = {str(l.get("og_record_id", "")).strip() for l in links}
    seen_in_crosswalk = reviewed | {str(g.get("og_record_id", "")).strip()
                                    for g in gaps if g.get("og_record_id")}
    for s in systems:
        rec = str(s.get("og_record_id", "")).strip()
        if rec and rec not in seen_in_crosswalk:
            gaps.append({
                "gap_type": "assessment not yet reviewed",
                "og_record_id": rec,
                "system_name_en": s.get("system_name_en", ""),
                "department_en": s.get("department_en", ""),
                "service_id": "", "service_name_en": "", "match_confidence": "",
                "reason": "Added to the master table after the crosswalk was prepared.",
                "candidate": "",
            })

    with open(DATA_DIR / "system_services.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(links[0].keys()))
        w.writeheader(); w.writerows(links)
    if gaps:
        with open(DATA_DIR / "crosswalk_gaps.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(gaps[0].keys()))
            w.writeheader(); w.writerows(gaps)

    log(f"  crosswalk: {len(links)} service link(s) across {matched} system(s)")
    unreviewed = sum(1 for g in gaps if g["gap_type"] == "assessment not yet reviewed")
    if unreviewed:
        log(f"  crosswalk: {unreviewed} assessment(s) added since it was prepared, not yet reviewed")
    return systems, gaps, matched


def assemble_core_tables() -> dict[str, list[dict]]:
    """
    The one place the core tables are put together.

    Returns {"systems": [...], "submissions": [...], "answers": [...]} with the
    validated PDF assessments merged in and the crosswalk attached. Returns an
    empty dict if step 4 has not been run.
    """
    systems = _read_optional("systems.csv")
    if not systems:
        return {}
    submissions = _read_optional("submissions.csv")
    answers = _read_optional("answers.csv")
    systems, submissions, answers, _ = merge_pdf_results(systems, submissions, answers)
    systems, _, _ = merge_crosswalk(systems)
    return {"systems": systems, "submissions": submissions, "answers": answers}


def log(msg=""):
    print(msg, flush=True)


def header(title: str):
    log("\n" + "=" * 74)
    log(title)
    log("=" * 74)
