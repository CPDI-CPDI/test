"""
STEP 6 — Triage the submissions that exist only as PDF.

Most records publish the tool's native JSON, which needs no interpretation. A
handful publish only PDFs, and those have to be read.

Publication date cannot tell you which questionnaire version a PDF was answered
under. Testing that assumption against the published records showed every
version's date range overlapping the next: v0.9.1 assessments were published
from 2021 to 2024 while v0.10.0 assessments were published from 2022 onward, and
the most recent record on the portal uses v0.10.0 while records published six
months earlier use v1.0.1. Departments complete an assessment long before it is
published, and some reuse a saved file from an older version.

So this script narrows the version structurally instead:

    1. the AIA PDF usually states its own version — that settles it outright
    2. otherwise, match the question wording found in the PDF against each
       catalog version and take the version that explains the most questions
    3. the publication date is reported alongside as corroboration only, never
       as the determinant

Extracting the text needs a PDF reader. pdftotext (poppler-utils) is used when
present, otherwise pypdf. Install whichever is easier:
    apt-get install poppler-utils     or     pip install pypdf

Outputs
    data/pdf_triage.csv   one row per PDF-only submission, with the evidence
"""
from __future__ import annotations
import re, shutil, subprocess, sys
from collections import defaultdict
from pathlib import Path
from common import (RAW_DIR, read_csv, write_csv, fetch, similarity, norm,
                    absolute_url, log, header)

VERSION_IN_PDF = re.compile(
    r"(?:^[ \t]*Version[ \t]*:[ \t]*v?[ \t]*([0-9][0-9A-Za-z.]*)"
    r"|(?:Algorithmic Impact Assessment|[ÉE]valuation de l['’]incidence algorithmique)"
    r"[ \t]*v[ \t]*([0-9]+(?:\.[0-9]+)*))", re.I | re.M)


def extract_text(path: Path) -> str:
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                 capture_output=True, timeout=120)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except ImportError:
        raise SystemExit("No PDF reader available. "
                         "Install poppler-utils (pdftotext) or run: pip install pypdf")
    except Exception as e:
        log(f"    ! could not read {path.name}: {e}")
        return ""


MIN_HITS = 10          # below this there is not enough evidence to call it
MIN_PROBES = 20        # catalogs smaller than this are not comparable on share


def catalog_groups(fields_by_version: dict) -> dict:
    """
    Some versions are byte-identical in content: v1.0.0 and v1.0.1 carry the
    same 295 questions. A tie between them is not ambiguity, it is the same
    questionnaire under two tags. Group them so the margin is measured against
    a genuinely different catalog.
    """
    sig_to_versions = defaultdict(list)
    for version, questions in fields_by_version.items():
        sig = frozenset(norm(q)[:80] for q in questions if len(norm(q)) >= 25)
        sig_to_versions[sig].append(version)
    group_of = {}
    for gid, (_, versions) in enumerate(sig_to_versions.items()):
        for v in versions:
            group_of[v] = gid
    return group_of


def fingerprint(text: str, fields_by_version: dict) -> list[tuple[str, float, int]]:
    """
    Score each catalog version by how much of its question wording appears.

    Ranking uses absolute hits rather than the matched share. Share alone
    favours small catalogs: an early version with only a few dozen distinctive
    questions can match a high proportion of them while explaining far less of
    the document than a later version would. Hits measure how much of the PDF
    the catalog actually accounts for; share is kept as a tiebreaker.
    """
    flat = norm(text)
    scored = []
    for version, questions in fields_by_version.items():
        seen, hits, probes = set(), 0, 0
        for q in questions:
            probe = norm(q)[:80]
            if len(probe) < 25 or probe in seen:
                continue
            seen.add(probe)
            probes += 1
            if probe in flat:
                hits += 1
        share = hits / probes if probes else 0.0
        if probes < MIN_PROBES:
            share = 0.0                      # too small a catalog to judge on share
        scored.append((version, round(share, 3), hits))
    return sorted(scored, key=lambda x: (-x[2], -x[1]))


def main():
    header("STEP 6 — Triaging PDF-only submissions")

    records = {r["og_record_id"]: r for r in read_csv("og_records.csv")}
    resources = read_csv("og_resources.csv")
    qmap = read_csv("question_map.csv")

    fields_by_version = defaultdict(list)
    for r in qmap:
        if r["text_en"]:
            fields_by_version[r["catalog_version"]].append(r["text_en"])
    log(f"  catalog versions to test against: {', '.join(sorted(fields_by_version))}")

    # records with no assessment JSON
    has_json = {r["og_record_id"] for r in resources
                if r["format"] == "JSON" and r["is_assessment_artifact"] == "Y"}
    candidates = [r for r in resources
                  if r["format"] == "PDF"
                  and r["is_assessment_artifact"] == "Y"
                  and r["language"] in ("en", "unknown")
                  and r["og_record_id"] not in has_json]
    # One PDF per record and submission is enough; records often carry the same
    # assessment more than once, and an English and an unknown-language copy of
    # the same file would otherwise both be triaged.
    targets, seen_keys = [], set()
    for r in sorted(candidates, key=lambda x: (x["og_record_id"], x["language"] != "en")):
        key = (r["og_record_id"], r["submission_label"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        targets.append(r)
    groups = catalog_groups(fields_by_version)
    dupes = defaultdict(list)
    for v, g in groups.items():
        dupes[g].append(v)
    for g, vs in dupes.items():
        if len(vs) > 1:
            log(f"  identical catalogs treated as one: {', '.join('v'+v for v in sorted(vs))}")
    log(f"  PDF-only submissions to triage  : {len(targets)}\n")

    rows = []
    for res in targets:
        rec = records.get(res["og_record_id"], {})
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", res["resource_name"])[:90]
        dest = RAW_DIR / "pdf" / f"{res['og_record_id']}__{name}.pdf"
        if not dest.exists():
            try:
                dest.write_bytes(fetch(absolute_url(res["download_url"]), binary=True))
                log(f"  downloaded {dest.name}")
            except Exception as e:
                rows.append({"og_record_id": res["og_record_id"],
                             "title": rec.get("title_en", ""),
                             "resource_name": res["resource_name"],
                             "status": f"download failed: {e}"})
                continue

        text = extract_text(dest)
        if not text.strip():
            rows.append({"og_record_id": res["og_record_id"],
                         "title": rec.get("title_en", ""),
                         "resource_name": res["resource_name"],
                         "status": "no extractable text — likely a scan, needs OCR"})
            continue

        stated = ""
        m = VERSION_IN_PDF.search(text)
        if m:
            stated = (m.group(1) or m.group(2) or "").strip()

        ranked = fingerprint(text, fields_by_version)
        best, best_share, best_hits = ranked[0] if ranked else ("", 0.0, 0)

        # Compare against the best result from a genuinely different catalog,
        # so identical versions under two tags do not read as ambiguity.
        best_group = groups.get(best)
        tied = [v for v, _, h in ranked if groups.get(v) == best_group and h == best_hits]
        runner_hits = next((h for v, _, h in ranked if groups.get(v) != best_group), 0)
        margin = best_hits - runner_hits

        # Where identical catalogs tie, take the most recent tag.
        if len(tied) > 1:
            best = sorted(tied, key=lambda v: [int(p) if p.isdigit() else 0
                                               for p in str(v).split(".")])[-1]

        if stated:
            resolved, source, conf = stated, "stated_in_pdf", "high"
        elif best_hits >= MIN_HITS and margin >= 5 and best_share >= 0.30:
            resolved, source, conf = best, "fingerprint", "medium"
        elif best_hits >= MIN_HITS:
            resolved, source, conf = best, "fingerprint", "low"
        else:
            resolved, source, conf = "", "unresolved", "none"

        rows.append({
            "og_record_id": res["og_record_id"],
            "title": rec.get("title_en", ""),
            "department": rec.get("department", ""),
            "resource_name": res["resource_name"],
            "submission_label": res["submission_label"],
            "publication_date": rec.get("record_released", ""),
            "version_stated_in_pdf": stated,
            "fingerprint_best_version": best,
            "fingerprint_match_share": best_share,
            "fingerprint_hits": best_hits,
            "margin_over_runner_up": margin,
            "ranking": "; ".join(f"v{v}:{h}hits" for v, _, h in ranked[:4]),
            "tied_identical_versions": "; ".join("v"+v for v in tied) if len(tied) > 1 else "",
            "resolved_version": resolved,
            "version_source": source,
            "confidence": conf,
            "text_chars": len(text),
            "status": "ok",
        })
        log(f"  {rec.get('title_en','')[:40]:<42} "
            f"stated={stated or '-':<8} best=v{best or '-'} "
            f"hits={best_hits:<4} share={best_share:.2f} margin={margin:<4} "
            f"-> v{resolved or 'UNRESOLVED'} [{conf}]")

    write_csv("pdf_triage.csv", rows)

    header("SUMMARY")
    by_conf = defaultdict(int)
    for r in rows:
        by_conf[r.get("confidence", "error")] += 1
    for k in ("high", "medium", "low", "none", "error"):
        if by_conf.get(k):
            log(f"  confidence {k:<8}: {by_conf[k]}")
    log("\n  'high' means the PDF states its own version and can be scored directly.")
    log("  'low' or 'none' needs a person to look. Record the decision in")
    log("  submissions.version_source rather than leaving it inferred.")
    log("\n  Publication dates are in the output for corroboration only. They are")
    log("  not reliable evidence of version — see the note at the top of this file.")


if __name__ == "__main__":
    main()
