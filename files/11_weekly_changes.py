"""
STEP 11 — What changed this run, and is it safe to publish?

Run by the weekly workflow after step 10. Compares the published tables from
before the run with the ones just written, and does two jobs:

  * Gate. A run that loses catalog versions, loses a large share of the
    submissions, or changes the schema version is stopped before anything is
    committed. Each has happened, or nearly happened, for a reason that had
    nothing to do with the AIAs themselves: a rate-limited tag listing leaves
    step 2 with one catalog, a portal outage leaves step 1 with few records.
    Publishing that would quietly empty the report.

  * Notice. New submissions, submissions whose version or impact level
    changed, withdrawn submissions and new questionnaire versions are written
    up as Markdown, for the issue the workflow opens and for the run summary.

The data is public on the Open Government portal and in this repository, so the
notice carries names, departments and levels as published.

    python 11_weekly_changes.py BEFORE_DIR AFTER_DIR NOTICE.md

Exit code 0 = safe to commit (the notice may be empty); 1 = stop.
Writes new_count, changed_count and notify to $GITHUB_OUTPUT when it is set.
"""
from __future__ import annotations
import csv, os, sys
from pathlib import Path

MAX_SUBMISSION_DROP_SHARE = 0.10   # losing more than this share stops the run
MAX_SUBMISSION_DROP_COUNT = 3      # ...unless it is no more than this many rows
PORTAL = "https://open.canada.ca/data/en/dataset/"


def rows(folder: Path, name: str) -> list[dict]:
    p = folder / f"{name}.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def schema_versions(folder: Path) -> set[str]:
    return {r.get("schema_version", "") for r in rows(folder, "_manifest")}


def gate(before: Path, after: Path) -> list[str]:
    """Reasons not to publish this run. Empty means it is safe."""
    problems = []
    if not rows(before, "submissions"):
        return problems                     # first run: nothing to compare with
    cv_b = {r["catalog_version"] for r in rows(before, "catalog_versions")}
    cv_a = {r["catalog_version"] for r in rows(after, "catalog_versions")}
    lost = sorted(cv_b - cv_a)
    if lost:
        problems.append(f"catalog versions disappeared: {', '.join(lost)} "
                        f"(step 2 could not list the questionnaire releases?)")
    n_b, n_a = len(rows(before, "submissions")), len(rows(after, "submissions"))
    drop = n_b - n_a
    if drop > MAX_SUBMISSION_DROP_COUNT and drop > MAX_SUBMISSION_DROP_SHARE * n_b:
        problems.append(f"submissions fell from {n_b} to {n_a} "
                        f"(step 1 could not reach the portal?)")
    sv_b, sv_a = schema_versions(before), schema_versions(after)
    if sv_b and sv_a and sv_b != sv_a:
        problems.append(f"schema version changed from {', '.join(sorted(sv_b))} to "
                        f"{', '.join(sorted(sv_a))}; publish a contract change by hand, "
                        f"not from the schedule")
    return problems


def describe(before: Path, after: Path):
    systems = {r["og_record_id"]: r for r in rows(after, "systems")}
    systems.update({k: v for k, v in
                    ((r["og_record_id"], r) for r in rows(before, "systems"))
                    if k not in systems})
    sub_b = {r["submission_id"]: r for r in rows(before, "submissions")}
    sub_a = {r["submission_id"]: r for r in rows(after, "submissions")}
    new = [sub_a[k] for k in sub_a if k not in sub_b]
    gone = [sub_b[k] for k in sub_b if k not in sub_a]
    changed = []
    for k in sub_a.keys() & sub_b.keys():
        a, b = sub_a[k], sub_b[k]
        diffs = [(col, b.get(col, ""), a.get(col, ""))
                 for col in ("catalog_version", "impact_level", "current_score")
                 if b.get(col, "") != a.get(col, "")]
        if diffs:
            changed.append((a, diffs))
    cv_b = {r["catalog_version"] for r in rows(before, "catalog_versions")}
    new_versions = [r["catalog_version"] for r in rows(after, "catalog_versions")
                    if cv_b and r["catalog_version"] not in cv_b]
    return systems, new, changed, gone, new_versions


def notice(systems, new, changed, gone, new_versions) -> str:
    def name(s):
        sy = systems.get(s["og_record_id"], {})
        return sy.get("system_name_en") or s["og_record_id"]

    def dept(s):
        return systems.get(s["og_record_id"], {}).get("department_en", "")

    out = []
    if new:
        out += [f"### {len(new)} new submission(s)", "",
                "| System | Department | Submission | Version | Impact level | Published | Source |",
                "|---|---|---|---|---|---|---|"]
        for s in sorted(new, key=lambda s: s.get("publication_date", ""), reverse=True):
            out.append(f"| [{name(s)}]({PORTAL}{s['og_record_id']}) | {dept(s)} | "
                       f"{s.get('submission_label', '')} | {s.get('catalog_version', '')} | "
                       f"{s.get('impact_level', '')} | {s.get('publication_date', '')} | "
                       f"{s.get('source_format', '')} |")
        out.append("")
    if changed:
        out += [f"### {len(changed)} existing submission(s) changed", ""]
        for s, diffs in changed:
            what = "; ".join(f"{c} {b or '-'} → {a or '-'}" for c, b, a in diffs)
            out.append(f"- [{name(s)}]({PORTAL}{s['og_record_id']}) ({dept(s)}): {what}")
        out.append("")
    if gone:
        out += [f"### {len(gone)} submission(s) no longer published", ""]
        for s in gone:
            out.append(f"- {name(s)} ({dept(s)}), {s.get('submission_label', '')}")
        out.append("")
    if new_versions:
        out += ["### New questionnaire version(s) in the catalog", "",
                ", ".join(new_versions),
                "", "Check `bridge_review.csv` and `registry_review.csv` in the workbook "
                "for questions matched with low confidence.", ""]
    return "\n".join(out)


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    before, after, notice_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

    problems = gate(before, after)
    systems, new, changed, gone, new_versions = describe(before, after)
    body = notice(systems, new, changed, gone, new_versions)

    mentions = os.environ.get("NOTIFY_GITHUB_USERS", "").strip()
    run_url = os.environ.get("RUN_URL", "")
    footer = []
    if run_url:
        footer.append(f"Run: {run_url}")
    if mentions:
        footer.append(f"cc {mentions}")
    notice_path.write_text((body + "\n" + "\n".join(footer)).strip() + "\n", encoding="utf-8")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## AIA weekly run\n\n")
            f.write(("**Stopped before publishing:**\n" + "\n".join(f"- {p}" for p in problems)
                     + "\n\n") if problems else "")
            f.write(body or "No new or changed submissions this week.\n")

    worth_telling = bool(new or changed or gone or new_versions)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"new_count={len(new)}\nchanged_count={len(changed)}\n")
            f.write(f"notify={'true' if worth_telling else 'false'}\n")

    print(f"new submissions: {len(new)}   changed: {len(changed)}   "
          f"withdrawn: {len(gone)}   new catalog versions: {len(new_versions)}")
    if problems:
        print("\nSTOPPING — this run will not be published:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)


if __name__ == "__main__":
    main()
