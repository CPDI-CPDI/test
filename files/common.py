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


# ---------------------------------------------------------------- branching
# A question's visibleIf condition, evaluated against the answers a submission
# holds. The catalogs use a small part of SurveyJS's expression language:
#   {field} = "item1"     {field} <> "item1"     {field} = 4
#   {field} contains "item1"     {field} contains ["item1"]
#   {field} empty / notempty
# joined by and / or, with brackets. Brackets matter: v1.0.x has conditions
# like (A or B) and (C or D), which read left to right without them give the
# wrong answer.
#
# Logic is three-valued. An atom that cannot be read is unknown, and an unknown
# result counts as met, so a question is never dropped on a rule we could not
# interpret.

_TOKEN = re.compile(r"""\s*(\(|\)|\band\b|\bor\b|\{[^}]+\}\s*(?:=|<>|!=|(?:not\s*)?contains|(?:not)?empty)"""
                    r"""\s*(?:"[^"]*"|'[^']*'|\[[^\]]*\]|-?\d+(?:\.\d+)?)?)""", re.I)
_ATOM = re.compile(r"""^\{([^}]+)\}\s*(=|<>|!=|(?:not\s*)?contains|(?:not)?empty)\s*(.*)$""", re.I)


def _literal_values(text: str) -> list[str] | None:
    t = text.strip()
    if not t:
        return []
    if t[0] in "\"'" and t[-1] == t[0]:
        return [t[1:-1]]
    if t.startswith("[") and t.endswith("]"):
        return re.findall(r"""["']([^"']*)["']""", t)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return [t]
    return None


def _atom(expr: str, held: dict, missing_is_empty: bool):
    m = _ATOM.match(expr.strip())
    if not m:
        return None
    field, op, rest = m.group(1).split(".")[0].strip(), m.group(2).lower(), m.group(3)
    got = held.get(field)
    if got is None:
        if not missing_is_empty:
            return None
        got = set()
    if op.endswith("empty"):
        empty = not got
        return (not empty) if op.startswith("not") else empty
    values = _literal_values(rest)
    if values is None:
        return None
    hit = all(v in got for v in values) if values else False
    if op in ("<>", "!="):
        return not hit
    if op.startswith("not"):
        return not hit
    return hit


def satisfies(condition: str, answers_by_field: dict, missing_is_empty: bool = False) -> bool:
    """
    Whether a branching condition is met.

    answers_by_field maps a field name to the set of option values it holds.
    With missing_is_empty, a field with no answer counts as empty (the tool's
    own reading: an unanswered question equals nothing); without it, a missing
    field makes the atom unknown, which is the cautious reading for a PDF where
    an answer may simply not have been extracted.
    """
    if not condition or not str(condition).strip():
        return True
    tokens = [t.strip() for t in _TOKEN.findall(condition) if t.strip()]
    if "".join(tokens).replace(" ", "") != re.sub(r"\s+", "", condition):
        return True                                  # something we do not parse
    pos = 0

    def expr():
        nonlocal pos
        left = term()
        while pos < len(tokens) and tokens[pos].lower() == "or":
            pos += 1
            right = term()
            left = True if (left is True or right is True) else \
                None if (left is None or right is None) else False
        return left

    def term():
        nonlocal pos
        left = factor()
        while pos < len(tokens) and tokens[pos].lower() == "and":
            pos += 1
            right = factor()
            left = False if (left is False or right is False) else \
                None if (left is None or right is None) else True
        return left

    def factor():
        nonlocal pos
        if pos >= len(tokens):
            return None
        t = tokens[pos]
        if t == "(":
            pos += 1
            v = expr()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return v
        pos += 1
        return _atom(t, answers_by_field, missing_is_empty)

    result = expr()
    return True if result is None else bool(result)


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
    max_mit = {r.get("catalog_version", ""): r.get("max_mitigation", "")
               for r in _read_optional("catalog_versions.csv")}

    def mitigation_pct(r):
        try:
            mx = float(max_mit.get(r.get("catalog_version", ""), "") or 0)
            return round(float(r.get("computed_mitigation") or 0) / mx * 100, 1) if mx else ""
        except ValueError:
            return ""

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
                "mitigation_pct": mitigation_pct(r),   # was left blank for PDFs
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


PHASE_FROM_VALUE = {"item1": "design", "item2": "implementation"}


def submission_phase(answers_for_submission, qmap) -> str:
    """
    Which mitigation pages a submission was shown: 'design' or 'implementation'.

    The questionnaire decides this from the project-phase answer (item1 is
    design, item2 implementation), so that is read first. PDFs carry the label
    rather than the value, and older files may lack it; then the phase whose
    mitigation pages hold more answers is used.
    """
    for a in answers_for_submission:
        if a.get("field_name") == "projectDetailsPhase":
            raw = str(a.get("answer_value_raw", "")).strip().lower()
            if raw in PHASE_FROM_VALUE:
                return PHASE_FROM_VALUE[raw]
            text = str(a.get("answer_text_en", "")).lower()
            for phase in ("implementation", "design"):
                if phase in text:
                    return phase
    counts = defaultdict(int)
    for a in answers_for_submission:
        m = qmap.get((a.get("catalog_version", ""), a.get("field_name", "")))
        if m and m.get("mitigation_phase"):
            counts[m["mitigation_phase"]] += 1
    return max(counts, key=counts.get) if counts else ""


def build_mitigation_areas(submissions, answers) -> list[dict]:
    """
    Mitigation points earned in each of the four areas, against the most that
    area could earn in that submission's version and phase.

    The maxima come from section_versions and, per phase, add up to the
    version's maximum mitigation score, so the four rows of a submission
    account for its whole mitigation score. Answers to questions that were not
    shown do not count, as in the scoring.
    """
    qmap = {(r["catalog_version"], r["field_name"]): r for r in _read_optional("question_map.csv")}
    area_max = defaultdict(float)
    for r in _read_optional("section_versions.csv"):
        if r.get("mitigation_area") and r.get("mitigation_phase"):
            key = (r["catalog_version"], r["mitigation_phase"], r["mitigation_area"])
            area_max[key] += float(r.get("max_mitigation_points") or 0)
    if not qmap or not area_max:
        return []

    by_sub = defaultdict(list)
    for a in answers:
        by_sub[a.get("submission_id", "")].append(a)

    rows = []
    for s in submissions:
        sid, ver = s.get("submission_id", ""), s.get("catalog_version", "")
        mine = by_sub.get(sid, [])
        phase = submission_phase(mine, qmap)
        earned = defaultdict(float)
        for a in mine:
            if a.get("shown") == "N":
                continue
            m = qmap.get((a.get("catalog_version", ""), a.get("field_name", "")))
            if m and m.get("point_type") == "mitigation" and m.get("mitigation_area"):
                try:
                    earned[m["mitigation_area"]] += float(a.get("points") or 0)
                except ValueError:
                    pass
        for (v, ph, area), mx in sorted(area_max.items()):
            if v != ver or ph != phase or mx <= 0:
                continue
            pts = earned.get(area, 0.0)
            rows.append({
                "submission_id": sid,
                "og_record_id": s.get("og_record_id", ""),
                "catalog_version": ver,
                "mitigation_phase": phase,
                "mitigation_area": area,
                "points": int(pts) if pts.is_integer() else pts,
                "max_points": int(mx) if mx.is_integer() else mx,
                "pct_of_max": round(min(pts / mx, 1.0) * 100, 1),
            })
    return rows


def _has_content(a: dict) -> bool:
    raw = str(a.get("answer_value_raw", "") or "").strip()
    if raw and raw not in ('""', "[]", "null", "{}"):
        return True
    return bool(str(a.get("answer_text_en", "") or "").strip())


def _held_values(a: dict, options: dict) -> set:
    """The option values an answer holds, in the form conditions compare against."""
    raw = str(a.get("answer_value_raw", "") or "").strip()
    if raw:
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        vals = val if isinstance(val, list) else [val]
        return {str(v) for v in vals if v not in (None, "")}
    # PDFs carry the label, not the value: map labels back to values, an exact
    # label first, then labels found inside a multi-select answer.
    text = norm(a.get("answer_text_en", ""))
    opts = options.get((a.get("catalog_version", ""), a.get("field_name", "")), [])
    vals = {o["option_value"] for o in opts if o.get("text_en") and norm(o["text_en"]) == text}
    if not vals:
        vals = {o["option_value"] for o in opts
                if o.get("text_en") and norm(o["text_en"]) and norm(o["text_en"]) in text}
    return vals or ({"__answered__"} if text else set())


def build_question_coverage(submissions, answers) -> list[dict]:
    """
    One row per submission per question in its version: was the question
    shown to the department, and did they answer it.

    A question is not shown when it sits on the mitigation pages of the other
    phase, when its parent was not shown, or when its branching condition is
    not met by the answers held. An unanswered parent counts as empty, which is
    how the questionnaire reads it. A condition that cannot be read counts as
    met, so the question is treated as shown rather than silently dropped.

    'Skipped' in the report is shown = Y and answered = N.
    """
    qrows = defaultdict(list)
    for r in _read_optional("question_map.csv"):
        qrows[r["catalog_version"]].append(r)
    if not qrows:
        return []
    for v in qrows:
        qrows[v].sort(key=lambda r: int(r.get("chain_depth") or 0))
    try:
        options = load_option_lookup()
    except SystemExit:
        options = {}

    by_sub = defaultdict(list)
    for a in answers:
        by_sub[a.get("submission_id", "")].append(a)
    qmap = {(r["catalog_version"], r["field_name"]): r for rs in qrows.values() for r in rs}

    out = []
    for s in submissions:
        sid, ver = s.get("submission_id", ""), s.get("catalog_version", "")
        mine = by_sub.get(sid, [])
        phase = submission_phase(mine, qmap)
        held, answered = {}, set()
        for a in mine:
            f = a.get("field_name", "")
            if _has_content(a):
                held.setdefault(f, set()).update(_held_values(a, options))
                if a.get("shown") != "N":
                    answered.add(f)
        shown = {}
        for r in qrows.get(ver, []):
            f = r["field_name"]
            parent = r.get("parent_field_name", "")
            if r.get("mitigation_phase") and phase and r["mitigation_phase"] != phase:
                state, why = "N", "other phase"
            elif parent and shown.get(parent) == "N":
                state, why = "N", "parent not shown"
            elif r.get("visible_if") and not satisfies(r["visible_if"], held, missing_is_empty=True):
                state, why = "N", "condition not met"
            else:
                state, why = "Y", ""
            shown[f] = state
            out.append({
                "submission_id": sid,
                "og_record_id": s.get("og_record_id", ""),
                "catalog_version": ver,
                "field_name": f,
                "qm_key": f"{ver}|{f}",
                "question_uid": r.get("question_uid", ""),
                "point_type": r.get("point_type", ""),
                "mitigation_area": r.get("mitigation_area", ""),
                "mitigation_phase": r.get("mitigation_phase", ""),
                "is_follow_up": "Y" if r.get("visible_if") else "N",
                "is_mandatory": r.get("is_mandatory", ""),
                "shown": state,
                "hidden_reason": why,
                "answered": "Y" if f in answered else "N",
            })
    return out


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
    coverage = build_question_coverage(submissions, answers)
    # Step 4 does not record whether a JSON answer's question was shown; the
    # coverage table now knows, so fill it in. PDF answers keep step 7's flag.
    shown = {(c["submission_id"], c["field_name"]): c["shown"] for c in coverage}
    for a in answers:
        if not a.get("shown"):
            a["shown"] = shown.get((a.get("submission_id", ""), a.get("field_name", "")), "")
    return {"systems": systems, "submissions": submissions, "answers": answers,
            "submission_mitigation_areas": build_mitigation_areas(submissions, answers),
            "question_coverage": coverage}


def log(msg=""):
    print(msg, flush=True)


def header(title: str):
    log("\n" + "=" * 74)
    log(title)
    log("=" * 74)
