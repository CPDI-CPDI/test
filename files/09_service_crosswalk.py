"""
STEP 09 — Bring in the Service Inventory crosswalk.

Links assessed systems to the services they support, using the GC Service
Inventory. The Inventory asks every service whether it uses an automated
decision system, so the services answering yes are the population that ought to
have an assessment behind them. Comparing the two lists in both directions is
the point: it shows assessments with no service named, and services declaring
automation with no assessment on record.

This step is different from every other one in the pipeline, and the difference
matters. Everything else is rebuilt from public sources with no human input, so
a rerun always reproduces the same tables. This crosswalk is judged by an
analyst: deciding that a particular assessment covers a particular service is a
reading of two descriptions, not a lookup. So the file is treated as a
maintained input, carried through with its confidence and rationale intact, and
never regenerated.

Because it is judged rather than derived, three things are kept with every row:
who decided, how confident they were, and why. A match nobody can explain later
is worse than no match.

Input
    A crosswalk workbook with a "Crosswalk" sheet and an "ADS services" sheet.
    Default: crosswalk/aia_service_inventory_crosswalk.xlsx

Outputs
    data/services.csv           one row per service declaring automation
    data/system_services.csv    one row per system-to-service link
    data/crosswalk_gaps.csv     both kinds of gap, with the reason
"""
from __future__ import annotations
import sys
from collections import defaultdict
from pathlib import Path
from common import BASE, DATA_DIR, read_csv, write_csv, log, header

# Looked for in several places so the file can simply be dropped beside the
# scripts rather than into a particular folder.
SEARCH = [
    BASE / "crosswalk",
    BASE,
    BASE / "data",
    Path.cwd(),
    Path(__file__).resolve().parent,
]
PATTERN = "*crosswalk*.xlsx"


def find_workbook():
    for folder in SEARCH:
        try:
            hits = sorted(folder.glob(PATTERN))
        except OSError:
            continue
        if hits:
            return hits[0]
    return None

# The workbook's own vocabulary, kept rather than renamed so that a reader can
# trace a row back to the sheet it came from.
MATCHED = "Clear AIA–service match"
AIA_ORPHAN = "AIA has no clear service match"
SERVICE_ORPHAN = "Service has no clear AIA match"


def load_sheet(wb, name):
    if name not in wb.sheetnames:
        raise SystemExit(f"the crosswalk workbook has no '{name}' sheet")
    rows = list(wb[name].iter_rows(values_only=True))
    head = [str(h) if h is not None else "" for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:]]


def clean(v):
    return "" if v is None else str(v).strip()


def main():
    header("STEP 09 — Service Inventory crosswalk")
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("openpyxl is needed for this step: pip install openpyxl")

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else find_workbook()
    if path is None or not path.exists():
        log("  No crosswalk workbook found. Looked for a file matching")
        log(f"  '{PATTERN}' in:")
        for folder in SEARCH:
            log(f"    {folder}")
        log("\n  Drop the workbook in one of those, or pass its path:")
        log("    python 09_service_crosswalk.py path/to/crosswalk.xlsx")
        log("\n  This step is optional. The rest of the pipeline does not need it.")
        return
    log(f"  reading {path.name}")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    cross = load_sheet(wb, "Crosswalk")
    ads = load_sheet(wb, "ADS services")
    log(f"  crosswalk rows {len(cross)}   services declaring automation {len(ads)}")

    # The systems we hold. A crosswalk built against an earlier run can name a
    # system that no longer exists, or miss one that has since been added, so
    # both directions are reported rather than assumed away.
    # Joined on the Open Government record identifier, not on system_id.
    # system_id is assigned by row order when the tables are built, so it moves
    # whenever a record is added or removed. The record identifier never moves,
    # so a crosswalk prepared months ago still lines up.
    try:
        systems = {clean(r["og_record_id"]): r for r in read_csv("systems.csv")}
    except SystemExit:
        raise SystemExit("run 04_ingest_submissions.py before this step")

    # ---------------------------------------------------------------- services
    services = []
    for r in ads:
        owner = clean(r.get("owner_org_title"))
        en, _, fr = owner.partition("|")
        services.append({
            "service_id": clean(r.get("service_id")),
            "fiscal_year": clean(r.get("fiscal_yr")),
            "service_name_en": clean(r.get("service_name_en")),
            "owner_org_code": clean(r.get("owner_org")),
            "owner_org_en": en.strip() or owner,
            "owner_org_fr": fr.strip(),
            "program_id": clean(r.get("program_id")),
            "program_name_en": clean(r.get("program_name_en")).strip('"'),
            "service_description_en": clean(r.get("service_description_en")),
            "declares_automation": clean(r.get("automated_decision_system")) or "Y",
            "automation_description_en": clean(r.get("automated_decision_system_description_en")),
            "service_uri_en": clean(r.get("service_uri_en")),
        })
    by_service = {s["service_id"]: s for s in services}

    # ---------------------------------------------------------------- links
    links, gaps = [], []
    seen_pairs = set()
    unknown_systems = set()

    for r in cross:
        status = clean(r.get("crosswalk_status"))
        rec = clean(r.get("aia_og_record_id"))
        sid = clean(r.get("aia_system_id"))
        svc = clean(r.get("service_id"))
        conf = clean(r.get("match_confidence"))
        why = clean(r.get("match_rationale"))

        if rec and rec not in systems:
            unknown_systems.add(rec)

        if status == MATCHED and rec and svc:
            key = (rec, svc)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            links.append({
                "og_record_id": rec,
                "service_id": svc,
                "system_id_at_crosswalk_time": sid,
                "match_confidence": conf,
                "match_rationale": why,
                "service_name_en": clean(r.get("service_name_en")) or
                                   by_service.get(svc, {}).get("service_name_en", ""),
                "service_fiscal_year": clean(r.get("service_fiscal_year")),
                "source": "analyst_review",
                "review_notes": clean(r.get("review_notes")),
                "system_in_master": "Y" if rec in systems else "N",
            })
        elif status == AIA_ORPHAN:
            gaps.append({
                "gap_type": "assessment without a named service",
                "og_record_id": rec,
                "system_name_en": clean(r.get("aia_system_name_en")),
                "department_en": clean(r.get("aia_department_en")),
                "service_id": "", "service_name_en": "",
                "match_confidence": conf,
                "reason": why or "No service in the Inventory was matched to this assessment.",
                "candidate": clean(r.get("candidate_or_related_record")),
            })
        elif status == SERVICE_ORPHAN:
            gaps.append({
                "gap_type": "service declaring automation without an assessment",
                "og_record_id": "", "system_name_en": "",
                "department_en": by_service.get(svc, {}).get("owner_org_en", ""),
                "service_id": svc,
                "service_name_en": clean(r.get("service_name_en")),
                "match_confidence": conf,
                "reason": why or "No published assessment was matched to this service.",
                "candidate": clean(r.get("candidate_or_related_record")),
            })

    # systems we hold that the crosswalk never mentions
    named = {clean(r.get("aia_og_record_id")) for r in cross}
    for rec, s in systems.items():
        if rec not in named:
            gaps.append({
                "gap_type": "assessment not yet reviewed",
                "og_record_id": rec,
                "system_name_en": s.get("system_name_en", ""),
                "department_en": s.get("department_en", ""),
                "service_id": "", "service_name_en": "",
                "match_confidence": "",
                "reason": "Added to the master table after the crosswalk was prepared.",
                "candidate": "",
            })

    write_csv("services.csv", services)
    write_csv("system_services.csv", links)
    write_csv("crosswalk_gaps.csv", gaps)

    # ---------------------------------------------------------------- summary
    header("SUMMARY")
    linked_systems = {l["og_record_id"] for l in links}
    linked_services = {l["service_id"] for l in links}
    by_conf = defaultdict(int)
    for l in links:
        by_conf[l["match_confidence"] or "unstated"] += 1

    log(f"  services declaring automation      : {len(services)}")
    log(f"  systems in the master table        : {len(systems)}")
    log(f"  confirmed links                    : {len(links)}")
    log(f"    covering systems                 : {len(linked_systems)}")
    log(f"    covering services                : {len(linked_services)}")
    log(f"    by confidence                    : {dict(by_conf)}")

    counts = defaultdict(int)
    for g in gaps:
        counts[g["gap_type"]] += 1
    log("")
    for k, v in sorted(counts.items()):
        log(f"  {k:<52} {v}")

    if unknown_systems:
        log(f"\n  the crosswalk names {len(unknown_systems)} record(s) not in the master table")
        log("  (records withdrawn from the portal, or a typo in the workbook)")

    unreviewed = counts.get("assessment not yet reviewed", 0)
    if unreviewed:
        log(f"\n  {unreviewed} assessment(s) have appeared since the crosswalk was prepared")
        log("  and need reviewing. They are listed in crosswalk_gaps.csv.")

    log("\n  The second gap type is the one worth attention: a service that says it")
    log("  uses an automated decision system, with no published assessment matched")
    log("  to it. That is a question to ask, not a finding \u2014 the assessment may")
    log("  exist under a different name, or the service may sit below the")
    log("  Directive's threshold.")


if __name__ == "__main__":
    main()
