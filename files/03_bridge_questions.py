"""
STEP 3 — Bridge questions and sections across catalog versions.

Assigns a stable numeric identity to each question and each section so that a
figure from a v0.9.1 assessment can sit beside one from v1.0.1 in the same
report. Field names cannot do this job: they change between versions, and the
same name is occasionally reused for a different question.

Matching runs in three passes, strongest evidence first:
    1. identical field name in an adjacent version
    2. near-identical question wording within the same section
    3. near-identical wording anywhere in the assessment

Every match records the method and a score. Weak matches are written out for
review rather than being quietly accepted; the reporting layer can exclude them.

Outputs
    data/questions.csv          one row per distinct question (question_uid)
    data/sections.csv           one row per distinct section (section_uid)
    data/question_map.csv       field_name + version -> question_uid, with evidence
    data/bridge_review.csv      matches below the confidence threshold
"""
from __future__ import annotations
import re
from collections import defaultdict
from common import (read_csv, write_csv, similarity, norm, version_key, log, header)

# Wording similarity required to treat two questions as the same question.
STRONG = 0.92     # accepted silently
WEAK = 0.78       # accepted but flagged for review
SECTION_MATCH = 0.80

# A branching condition names the question it depends on:
#     {aboutAlgorithm1} = 'item2-4'
#     {businessDrivers1} contains 'item6'
# That reference is the parent. Chains form naturally, so a follow-up to a
# follow-up sits two levels down without anything being authored by hand.
RE_FIELD_REF = re.compile(r"\{([^}]+)\}")


def parent_of(visible_if: str, known_fields: set) -> str:
    """
    The question a follow-up hangs from. Where a condition names several
    questions, the first recognised one is taken as the parent and the rest are
    kept as additional conditions rather than as parents: a question has one
    place in the chain, even when several answers control whether it appears.
    """
    for ref in RE_FIELD_REF.findall(visible_if or ""):
        field = ref.split(".")[0].strip()
        if field in known_fields:
            return field
    return ""


def build_chains(fields_by_version: dict):
    """
    Resolve each question's parent, then walk upward to find its root and depth.
    A question with no branching condition is its own root at depth 0.
    """
    out = {}
    for version, rows in fields_by_version.items():
        known = {r["field_name"] for r in rows}
        by_name = {r["field_name"]: r for r in rows}
        parent = {r["field_name"]: parent_of(r.get("visible_if", ""), known) for r in rows}

        for name in by_name:
            seen, depth, cur = {name}, 0, name
            while parent.get(cur):
                nxt = parent[cur]
                if nxt in seen:          # a cycle would otherwise loop forever
                    break
                seen.add(nxt)
                cur, depth = nxt, depth + 1
            out[(version, name)] = {
                "parent_field_name": parent.get(name, ""),
                "root_field_name": cur,
                "chain_depth": depth,
            }
    return out


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
# Sections are matched on their page name rather than their displayed title.
# The three de-risking areas — Data Quality, Procedural Fairness and Privacy —
# all carry the title "De-Risking and Mitigation Measures", so matching on the
# title collapses six distinct pages into one section. The page name keeps them
# apart. Punctuation and case differ between versions ("project-details" versus
# "projectDetails"), so both are stripped before comparing.
def page_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def bridge_sections(sections):
    by_ver = defaultdict(list)
    for s in sections:
        by_ver[s["catalog_version"]].append(s)
    versions = sorted(by_ver, key=version_key)

    canon, rows, uid = [], [], 0
    for v in versions:
        for s in sorted(by_ver[v], key=lambda x: int(x["display_order"])):
            key = page_key(s.get("page_name"))
            best, best_score, method = None, 0.0, ""
            for cand in canon:
                if key and key == cand["_key"]:
                    best, best_score, method = cand, 1.0, "page_name"
                    break
            if best is None and not key:
                # Only reached when a version gives no page name at all. Titles
                # are a weak signal here: the three de-risking areas share one
                # title exactly, so matching on it would merge them back
                # together. Restricted to the same scoring role.
                for cand in canon:
                    if cand["point_type"] != s["point_type"]:
                        continue
                    sc = similarity(cand["name_en"], s["section_name_en"])
                    if sc > best_score:
                        best, best_score, method = cand, sc, "fuzzy_name"
                if best_score < SECTION_MATCH:
                    best = None

            if best is not None:
                sec_uid = best["section_uid"]
                best["last_seen_version"] = v
                if method == "fuzzy_name":
                    best["bridged_with_fuzzy_match"] = "Y"
            else:
                uid += 1
                sec_uid = uid
                canon.append({
                    "section_uid": uid,
                    "name_en": s["section_name_en"],
                    "name_fr": s["section_name_fr"],
                    "page_name": s.get("page_name", ""),
                    "_key": key,
                    "display_order": int(s["display_order"]),
                    "point_type": s["point_type"],
                    "mitigation_phase": s.get("mitigation_phase", ""),
                    "first_seen_version": v,
                    "last_seen_version": v,
                    "bridged_with_fuzzy_match": "N",
                })
                best_score, method = 1.0, "new_section"

            rows.append({
                "section_uid": sec_uid,
                "catalog_version": v,
                "page_name": s["page_name"],
                "section_name_en": s["section_name_en"],
                "section_name_fr": s["section_name_fr"],
                "display_order": s["display_order"],
                "point_type": s["point_type"],
                "mitigation_phase": s.get("mitigation_phase", ""),
                "max_raw_points": s["max_raw_points"],
                "max_mitigation_points": s["max_mitigation_points"],
                "match_method": method,
                "match_score": round(best_score, 3),
            })
    for cnd in canon:
        cnd.pop("_key", None)
    return canon, rows


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------
def bridge_questions(fields, section_map, chains):
    sec_lookup = {(r["catalog_version"], r["page_name"]): r["section_uid"]
                  for r in section_map}
    by_ver = defaultdict(list)
    for f in fields:
        by_ver[f["catalog_version"]].append(f)
    versions = sorted(by_ver, key=version_key)

    canon = []                       # canonical questions
    by_name = defaultdict(list)      # field_name -> canonical entries
    mapping, review, uid = [], [], 0

    # Wording alone cannot separate a follow-up asked in several places.
    # "Is this information publicly available?" appears under a different parent
    # each time, so the parent's wording is folded into the comparison. Without
    # it, eight distinct questions collapse into one.
    def signature(f, version):
        ch = chains.get((version, f["field_name"]), {})
        p = ch.get("parent_field_name")
        parent_text = ""
        if p:
            pf = next((x for x in by_ver[version] if x["field_name"] == p), None)
            if pf:
                parent_text = pf.get("text_en", "")
        return f["text_en"], parent_text

    def compare(a_text, a_parent, b_text, b_parent):
        s = similarity(a_text, b_text)
        if not a_parent and not b_parent:
            return s
        # Both are follow-ups: their parents must agree too.
        return 0.6 * s + 0.4 * similarity(a_parent, b_parent)

    for v in versions:
        for f in by_ver[v]:
            name = f["field_name"]
            sec_uid = sec_lookup.get((v, f["page_name"]))
            f_text, f_parent = signature(f, v)
            target, score, method = None, 0.0, ""

            # pass 1 — same field name, and wording has not diverged
            for c in by_name.get(name, []):
                s = similarity(c["canonical_text_en"], f["text_en"])
                if s >= WEAK or c["point_type"] == f["point_type"]:
                    target, score, method = c, max(s, 0.95), "field_name"
                    break

            # pass 2 — same section, near-identical wording and parent
            if target is None:
                for c in canon:
                    if c["section_uid"] != sec_uid:
                        continue
                    s = compare(c["canonical_text_en"], c.get("_parent_text", ""),
                                f_text, f_parent)
                    if s > score:
                        target, score, method = c, s, "text_and_parent"
                if score < WEAK:
                    target = None

            # pass 3 — near-identical wording and parent anywhere
            if target is None:
                for c in canon:
                    s = compare(c["canonical_text_en"], c.get("_parent_text", ""),
                                f_text, f_parent)
                    if s > score:
                        target, score, method = c, s, "text_global"
                if score < WEAK:
                    target = None

            if target is None:
                uid += 1
                target = {
                    "question_uid": uid,
                    "canonical_text_en": f["text_en"],
                    "canonical_text_fr": f["text_fr"],
                    "_parent_text": f_parent,
                    "section_uid": sec_uid,
                    "point_type": f["point_type"],
                    "answer_type": f["answer_type"],
                    "first_seen_version": v,
                    "last_seen_version": v,
                    "version_count": 0,
                    "field_names": name,
                    "reworded": "N",
                }
                canon.append(target)
                by_name[name].append(target)
                score, method = 1.0, "new_question"
            else:
                target["last_seen_version"] = v
                if name not in target["field_names"].split("; "):
                    target["field_names"] += f"; {name}"
                    by_name[name].append(target)
                if score < 1.0 and method != "field_name":
                    target["reworded"] = "Y"

            target["version_count"] += 1

            ch = chains.get((v, name), {})
            # Carry every column from the parsed question through, then add the
            # cross-version identity on top. question_fields and question_map
            # were the same grain, so keeping both meant two rows describing one
            # question in one version. This is the single per-version table.
            row = dict(f)
            row.update({
                "question_uid": target["question_uid"],
                "parent_field_name": ch.get("parent_field_name", ""),
                "root_field_name": ch.get("root_field_name", name),
                "chain_depth": ch.get("chain_depth", 0),
                "section_uid": sec_uid,
                "match_method": method,
                "match_score": round(score, 3),
                "needs_review": "Y" if (method != "new_question" and score < STRONG) else "N",
            })
            mapping.append(row)
            if row["needs_review"] == "Y":
                review.append(row)

    uid_by_field = {(r["catalog_version"], r["field_name"]): r["question_uid"] for r in mapping}
    for r in mapping:
        p = r.get("parent_field_name")
        r["parent_question_uid"] = uid_by_field.get((r["catalog_version"], p), "") if p else ""
        r["root_question_uid"] = uid_by_field.get(
            (r["catalog_version"], r.get("root_field_name")), r["question_uid"])

    parent_uid = {}
    for r in mapping:
        if r["parent_question_uid"]:
            parent_uid.setdefault(r["question_uid"], r["parent_question_uid"])
    for c in canon:
        c["status"] = "active" if c["last_seen_version"] == versions[-1] else "retired"
        c["parent_question_uid"] = parent_uid.get(c["question_uid"], "")
        c["is_follow_up"] = "Y" if parent_uid.get(c["question_uid"]) else "N"
        c.pop("_parent_text", None)
    return canon, mapping, review


def main():
    header("STEP 3 — Bridging questions and sections across versions")

    sections = read_csv("sections_raw.csv")
    fields = read_csv("question_fields.csv")
    versions = sorted({f["catalog_version"] for f in fields}, key=version_key)
    log(f"  versions: {', '.join(versions)}")
    log(f"  question rows across all versions: {len(fields)}")

    canon_sec, sec_map = bridge_sections(sections)
    log(f"\n  distinct sections after bridging: {len(canon_sec)}")
    fuzzy = [s for s in canon_sec if s["bridged_with_fuzzy_match"] == "Y"]
    for s in canon_sec:
        log(f"    {s['section_uid']:>3}  {s['name_en'][:46]:<48}"
            f"{s['first_seen_version']} -> {s['last_seen_version']}"
            + ("   (fuzzy)" if s["bridged_with_fuzzy_match"] == "Y" else ""))

    chains = build_chains({v: [f for f in fields if f["catalog_version"] == v]
                           for v in {f["catalog_version"] for f in fields}})
    canon_q, q_map, review = bridge_questions(fields, sec_map, chains)

    write_csv("sections.csv", canon_sec)
    write_csv("section_versions.csv", sec_map)
    write_csv("questions.csv", canon_q)
    write_csv("question_map.csv", q_map)
    write_csv("bridge_review.csv", review)

    header("SUMMARY")
    log(f"  distinct questions (question_uid) : {len(canon_q)}")
    log(f"  field-name mappings               : {len(q_map)}")
    log(f"  questions present in every version: "
        f"{sum(1 for c in canon_q if c['version_count'] >= len(versions))}")
    log(f"  questions reworded between versions: "
        f"{sum(1 for c in canon_q if c['reworded'] == 'Y')}")
    log(f"  retired questions                 : "
        f"{sum(1 for c in canon_q if c['status'] == 'retired')}")
    log(f"  sections bridged by fuzzy match   : {len(fuzzy)}")
    log(f"  matches below {STRONG} confidence     : {len(review)}  -> bridge_review.csv")

    depths = [int(r["chain_depth"]) for r in q_map]
    followups = sum(1 for r in q_map if r["parent_field_name"])
    log("")
    log(f"  follow-up questions (have a parent): {followups} of {len(q_map)}")
    if depths:
        log(f"  deepest chain                     : {max(depths)} levels")
        for d in range(0, max(depths) + 1):
            n = sum(1 for x in depths if x == d)
            if n:
                log(f"    depth {d}: {n} questions" + ("   (asked outright)" if d == 0 else ""))
    orphan = sum(1 for r in q_map if r["visible_if"] and not r["parent_field_name"])
    if orphan:
        log(f"  branching rules naming no known question: {orphan}")
    if review:
        log("\n  Review these before trusting cross-version comparisons:")
        for r in sorted(review, key=lambda x: x["match_score"])[:15]:
            log(f"    {r['match_score']:.2f}  v{r['catalog_version']:<8} {r['field_name'][:26]:<28}"
                f"{r['text_en'][:52]}")
    log("\nNext: python 04_ingest_submissions.py")


if __name__ == "__main__":
    main()
