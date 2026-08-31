"""
STEP 2 — Build the catalog layer from the AIA source questionnaire.

Fetches src/survey-enfr.json from the canada-ca/aia-eia-js repository at every
released tag, parses each into sections, question fields and answer options, and
computes the maximum attainable raw and mitigation scores for each version.

Those maxima are the reason this step matters: raw scores are not comparable
across AIA versions, so every figure in the reporting layer is expressed as a
percentage of the maximum for the version it was answered under.

This step also lifts the point value out of the answer value. The live tool
encodes a score inside the value itself ('item1-4' is worth 4), which means
changing a question's weighting silently invalidates every saved file. Here the
score becomes its own column.

Outputs
    data/catalog_versions.csv
    data/sections_raw.csv        per version, bridged in step 3
    data/question_fields.csv     per version, one row per question
    data/question_options.csv    one row per answer option
    raw/catalogs/*.json          the source files, untouched

Usage
    python 02_parse_catalogs.py                # every tag, plus master
    python 02_parse_catalogs.py v0.10.0 master # only these refs
"""
from __future__ import annotations
import json, re, sys
from collections import Counter, defaultdict
from common import (GITHUB_TAGS, SURVEY_RAW, RAW_DIR, PANEL_SUFFIX_POINT_TYPE,
                    MITIGATION_PAGE_HINTS, bilingual, option_points,
                    mitigation_phase, clean_version,
                    fetch, fetch_json, write_csv, log, header)

VERSION_RE = re.compile(r"Algorithmic Impact Assessment\s*v?\s*([0-9][0-9.]*)", re.I)
SUFFIX_RE = re.compile(r"-([A-Z]{2})$")


def detect_version(survey: dict, fallback: str) -> str:
    """The tool states its version in the welcome page HTML."""
    for page in survey.get("pages", []):
        for el in page.get("elements", []):
            html = el.get("html")
            text = bilingual(html) or ""
            m = VERSION_RE.search(text)
            if m:
                return m.group(1)
    return fallback


def walk_elements(elements, panel=None):
    """Flatten nested panels, remembering the panel each question sits in."""
    for el in elements or []:
        if el.get("type") == "panel":
            yield from walk_elements(el.get("elements"), panel=el.get("name", panel))
        else:
            yield el, panel


def panel_point_type(panel_name: str | None, page_name: str) -> tuple[str, str]:
    """Return (point_type, suffix)."""
    suffix = ""
    if panel_name:
        m = SUFFIX_RE.search(panel_name)
        if m:
            suffix = m.group(1)
    page_low = (page_name or "").lower()
    if any(h in page_low for h in MITIGATION_PAGE_HINTS):
        # A mitigation page still marks unscored panels with -NS.
        if suffix and PANEL_SUFFIX_POINT_TYPE.get(suffix) == "none":
            return "none", suffix
        return "mitigation", suffix
    return PANEL_SUFFIX_POINT_TYPE.get(suffix, "unknown" if suffix else "none"), suffix


def parse_survey(survey: dict, version: str):
    sections, fields, options = [], [], []
    suffixes = Counter()
    fid = oid = 0

    for order, page in enumerate(survey.get("pages", []), start=1):
        page_name = page.get("name", f"page{order}")
        title_en = bilingual(page.get("title")) or page_name
        title_fr = bilingual(page.get("title"), "fr") or ""
        if page_name == "welcome":
            continue

        sec_pts = defaultdict(int)
        phase = mitigation_phase(page_name)
        for el, panel in walk_elements(page.get("elements")):
            qtype = el.get("type")
            if qtype in ("html", "expression", "image"):
                continue
            ptype, suffix = panel_point_type(panel, page_name)
            if suffix:
                suffixes[suffix] += 1

            fid += 1
            choices = el.get("choices") or []
            max_pts = 0
            for idx, ch in enumerate(choices):
                val = ch.get("value") if isinstance(ch, dict) else ch
                pts = option_points(val)
                oid += 1
                options.append({
                    "option_id": oid,
                    "field_id": fid,
                    "catalog_version": version,
                    "field_name": el.get("name"),
                    "option_value": val,
                    "option_index": idx,
                    "points": pts,
                    "text_en": bilingual((ch or {}).get("text") if isinstance(ch, dict) else None) or str(val),
                    "text_fr": bilingual((ch or {}).get("text") if isinstance(ch, dict) else None, "fr") or "",
                })
                # checkboxes can accumulate every option; single-choice takes the highest
                if qtype == "checkbox":
                    max_pts += pts
                else:
                    max_pts = max(max_pts, pts)

            if ptype in ("raw", "mitigation"):
                sec_pts[ptype] += max_pts

            fields.append({
                "field_id": fid,
                "catalog_version": version,
                "field_name": el.get("name"),
                "page_name": page_name,
                "section_name_en": title_en,
                "section_name_fr": title_fr,
                "section_order": order,
                "panel_name": panel or "",
                "panel_suffix": suffix,
                "point_type": ptype,
                "mitigation_phase": phase if ptype == "mitigation" else "",
                "answer_type": qtype,
                "is_mandatory": "Y" if el.get("isRequired") else "N",
                "visible_if": el.get("visibleIf", ""),
                "option_count": len(choices),
                "max_points": max_pts,
                "text_en": bilingual(el.get("title")) or el.get("name"),
                "text_fr": bilingual(el.get("title"), "fr") or "",
                "guidance_en": bilingual(el.get("description")) or "",
                "guidance_fr": bilingual(el.get("description"), "fr") or "",
            })

        sections.append({
            "catalog_version": version,
            "page_name": page_name,
            "section_name_en": title_en,
            "section_name_fr": title_fr,
            "display_order": order,
            "max_raw_points": sec_pts["raw"],
            "max_mitigation_points": sec_pts["mitigation"],
            "mitigation_phase": phase if sec_pts["mitigation"] else "",
            "point_type": ("mitigation" if sec_pts["mitigation"] and not sec_pts["raw"]
                           else "raw" if sec_pts["raw"] else "none"),
        })

    return sections, fields, options, suffixes


def normalise_options(options, fields):
    """
    Split the answer choices into reference tables instead of one wide one.

    Almost every question offers the same handful of choice sets, and the same
    handful of scoring patterns. Yes/No appears well over a thousand times,
    worth 0/1 in some questions and 0/2 or 0/4 in others. Storing the labels
    once, the scoring patterns once, and linking each question to both leaves
    one row per question rather than one per choice.

      option_sets / option_set_items   the distinct label sets, stored once
      option_point_profiles            the distinct scoring patterns
      question_options                 one row per question: its set and pattern
      departments                      the organisation list, on its own

    Points are never lost: the pattern holds the score for each position, and
    the position maps back through the label set.
    """
    departments, seen_dept = [], {}
    sets, set_items = {}, []
    profiles, profile_items = {}, []
    links = []

    by_field = defaultdict(list)
    for o in options:
        by_field[(o["catalog_version"], o["field_name"])].append(o)

    for (version, field), opts in sorted(by_field.items()):
        opts = sorted(opts, key=lambda o: int(o.get("option_index") or 0))

        # The organisation list is a reference table, not an answer set. Its
        # field is named differently in different versions, so it is recognised
        # by name rather than assumed.
        if "department" in (field or "").lower() and len(opts) > 20:
            for o in opts:
                code = re.sub(r"^item", "", str(o["option_value"] or "")).strip()
                if code and code not in seen_dept:
                    seen_dept[code] = True
                    departments.append({
                        "department_code": code,
                        "name_en": o["text_en"],
                        "name_fr": o["text_fr"],
                        "first_seen_version": version,
                    })
            links.append({
                "catalog_version": version, "field_name": field,
                "field_id": opts[0].get("field_id", ""),
                "option_set_id": "DEPARTMENTS", "point_profile_id": "",
                "option_count": len(opts), "max_points": 0,
            })
            continue

        # Keyed on both languages: several questions share an English label set
        # while wording the French differently, and keying on English alone
        # would hand them all the first French wording seen.
        label_key = tuple(((o["text_en"] or "").strip(), (o["text_fr"] or "").strip())
                          for o in opts)
        if label_key not in sets:
            sid = len(sets) + 1
            sets[label_key] = sid
            for o in opts:
                set_items.append({
                    "option_set_id": sid,
                    "option_index": int(o.get("option_index") or 0),
                    "text_en": (o["text_en"] or "").strip(),
                    "text_fr": (o["text_fr"] or "").strip(),
                })
        sid = sets[label_key]

        # The stored value is part of the pattern, not just the score. Two
        # questions can offer the same labels for the same points while using
        # different values ("item1-0" against "item1"), and the value is what
        # appears in a saved file, so mixing them would break answer lookup.
        point_key = (sid, tuple((str(o["option_value"]), int(float(o.get("points") or 0)))
                                for o in opts))
        if point_key not in profiles:
            pid = len(profiles) + 1
            profiles[point_key] = pid
            for o in opts:
                profile_items.append({
                    "point_profile_id": pid,
                    "option_set_id": sid,
                    "option_index": int(o.get("option_index") or 0),
                    "option_value": o["option_value"],
                    "points": int(float(o.get("points") or 0)),
                })
        pid = profiles[point_key]

        links.append({
            "catalog_version": version,
            "field_name": field,
            "field_id": opts[0].get("field_id", ""),
            "option_set_id": sid,
            "point_profile_id": pid,
            "option_count": len(opts),
            "max_points": max((int(float(o.get("points") or 0)) for o in opts), default=0),
        })

    set_rows = [{"option_set_id": sid,
                 "option_count": sum(1 for i in set_items if i["option_set_id"] == sid),
                 "labels": " / ".join(en for en, _fr in key if en)[:120]}
                for key, sid in sets.items()]
    profile_rows = [{"point_profile_id": pid, "option_set_id": key[0],
                     "points_pattern": "/".join(str(p) for _v, p in key[1])}
                    for key, pid in profiles.items()]
    return set_rows, set_items, profile_rows, profile_items, links, departments


def main():
    header("STEP 2 — Building the catalog layer")

    refs = sys.argv[1:]
    if not refs:
        log("Listing released tags...")
        try:
            refs = [t["name"] for t in fetch_json(GITHUB_TAGS)]
        except Exception as e:
            log(f"  could not list tags ({e}); falling back to master only")
            refs = []
        refs.append("master")
    log(f"  refs to parse: {', '.join(refs)}\n")

    all_sec, all_fld, all_opt, all_ver = [], [], [], []
    seen_versions, all_suffixes = {}, Counter()

    for ref in refs:
        url = SURVEY_RAW.format(ref=ref)
        log(f"{ref}")
        try:
            body = fetch(url)
            survey = json.loads(body)
        except Exception as e:
            log(f"  ! skipped: {e}")
            continue

        version = clean_version(detect_version(survey, fallback=clean_version(ref)))
        if version in seen_versions:
            log(f"  version {version} already parsed from {seen_versions[version]}; skipped")
            continue
        seen_versions[version] = ref

        (RAW_DIR / "catalogs" / f"survey-{version}.json").write_text(body, encoding="utf-8")
        sec, fld, opt, suf = parse_survey(survey, version)
        all_sec += sec; all_fld += fld; all_opt += opt; all_suffixes += suf

        max_raw = sum(s["max_raw_points"] for s in sec)
        # Design and Implementation mitigation pages are alternatives, not
        # additions: an assessment answers one set according to its project
        # phase. The attainable maximum is the larger branch plus anything
        # shared, never the sum of both.
        mit_by_phase = defaultdict(int)
        for s in sec:
            if s["max_mitigation_points"]:
                mit_by_phase[s["mitigation_phase"] or "shared"] += s["max_mitigation_points"]
        shared = mit_by_phase.get("shared", 0)
        design = mit_by_phase.get("design", 0)
        implementation = mit_by_phase.get("implementation", 0)
        max_mit = shared + max(design, implementation)

        unknown = sum(1 for f in fld if f["point_type"] == "unknown")
        all_ver.append({
            "catalog_version": version,
            "git_ref": ref,
            "question_count": len(fld),
            "scored_question_count": sum(1 for f in fld if f["point_type"] in ("raw", "mitigation")),
            "section_count": len(sec),
            "max_raw": max_raw,
            "max_mitigation": max_mit,
            "max_mitigation_design": shared + design,
            "max_mitigation_implementation": shared + implementation,
            "max_mitigation_if_summed": shared + design + implementation,
            "mitigation_threshold": round(max_mit * 0.8, 1),
            "unclassified_questions": unknown,
            "source_url": url,
        })
        log(f"  v{version}: {len(fld)} questions, {len(sec)} sections, "
            f"max raw {max_raw}, max mitigation {max_mit} "
            f"(design {shared+design} / implementation {shared+implementation})"
            + (f"  [{unknown} unclassified]" if unknown else ""))

    write_csv("catalog_versions.csv", all_ver)
    write_csv("sections_raw.csv", all_sec)
    write_csv("question_fields.csv", all_fld)
    write_csv("question_options_wide.csv", all_opt)

    sets, items, profiles, prof_items, links, departments = normalise_options(all_opt, all_fld)
    write_csv("option_sets.csv", sets)
    write_csv("option_set_items.csv", items)
    write_csv("option_point_profiles.csv", profiles)
    write_csv("option_point_profile_items.csv", prof_items)
    write_csv("question_options.csv", links)
    write_csv("departments.csv", sorted(departments, key=lambda d: str(d["department_code"])))
    after = len(items) + len(prof_items) + len(links) + len(departments)
    log("\n  answer choices normalised:")
    log(f"    distinct label sets       : {len(sets)}  ({len(items)} labels stored once)")
    log(f"    distinct scoring patterns : {len(profiles)}  ({len(prof_items)} entries)")
    log(f"    questions                 : {len(links)}  (one row each)")
    log(f"    departments               : {len(departments)}  (own reference table)")
    log(f"    rows before / after       : {len(all_opt)} / {after}")
    log(f"    bilingual labels stored   : {len(all_opt)} / {len(items)}  "
        f"({len(items)/max(len(all_opt),1)*100:.0f}% of before)")

    header("PANEL SUFFIXES FOUND")
    for s, n in all_suffixes.most_common():
        mapped = PANEL_SUFFIX_POINT_TYPE.get(s, "*** UNMAPPED ***")
        log(f"  -{s}  {n:>5} questions  -> {mapped}")
    if any(s not in PANEL_SUFFIX_POINT_TYPE for s in all_suffixes):
        log("\n  An unmapped suffix means those questions were not scored. Add it to")
        log("  PANEL_SUFFIX_POINT_TYPE in common.py and run this step again.")

    header("CHECK THESE MAXIMA AGAINST THE PUBLISHED RECORDS")
    log("  Derived independently from the published scores:")
    log("    v0.10.0  max raw 124-133   max mitigation 46 (pinned)")
    log("    v0.9.1   max raw  96-106   max mitigation 45 (pinned)")
    log("    v1.0.1                     max mitigation 71-86 (bounded)")
    log("")
    expected = {"0.10.0": 46, "0.9.1": 45}
    for v in all_ver:
        want = expected.get(v["catalog_version"])
        if want is None:
            continue
        got = v["max_mitigation"]
        verdict = "matches" if got == want else f"DISAGREES (expected {want})"
        log(f"    v{v['catalog_version']}: computed max mitigation {got} -> {verdict}")
        if got != want:
            log(f"      summing both phases would give {v['max_mitigation_if_summed']};")
            log(f"      design branch {v['max_mitigation_design']}, "
                f"implementation branch {v['max_mitigation_implementation']}")
    log("")
    log("  A disagreement means the point-type or phase mapping needs adjusting")
    log("  in common.py. Everything downstream inherits these numbers.")
    log("\nNext: python 03_bridge_questions.py")


if __name__ == "__main__":
    main()
