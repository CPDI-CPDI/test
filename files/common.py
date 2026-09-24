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


def log(msg=""):
    print(msg, flush=True)


def header(title: str):
    log("\n" + "=" * 74)
    log(title)
    log("=" * 74)
