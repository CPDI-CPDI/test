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
    registry/question_registry.csv   lasting question_uid per (version, field); commit it
    registry/section_registry.csv    lasting section_uid per (version, page); commit it
    data/registry_review.csv    where matching disagrees with the registry
    data/question_map.csv       field_name + version -> question_uid, with evidence
    data/bridge_review.csv      matches below the confidence threshold
"""
from __future__ import annotations
import re
from collections import defaultdict
import csv
from common import (BASE, read_csv, write_csv, similarity, norm, version_key, log, header)

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
    key = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    # v0.5 prefixed the de-risking pages with "rm-" ("rm-data-quality-design");
    # from v0.6 they are "dataQualityDesign". Without dropping the prefix the
    # same page bridged as two sections and v0.5 never joined the later ones.
    return re.sub(r"^rm(?=consultation|dataquality|fairness|privacy)", "", key)


# The four areas mitigation is scored in. The questionnaire gives three of them
# the same title, so the area is read from the page name. v0.3 and v0.4 put all
# mitigation on two combined pages; a question there takes its area from the
# page it moved to in later versions (see assign_mitigation_areas).
MITIGATION_AREAS = (("consultation", "Consultation"), ("dataquality", "Data quality"),
                    ("fairness", "Procedural fairness"), ("privacy", "Privacy"))
COMBINED_AREA = "Combined page (v0.3-0.4)"


def mitigation_area(page_name: str) -> str:
    """'Consultation', 'Data quality', 'Procedural fairness', 'Privacy', or ''."""
    key = page_key(page_name)
    for stem, label in MITIGATION_AREAS:
        if key.startswith(stem):
            return label
    return ""


def is_combined_mitigation_page(page_name: str) -> bool:
    return page_key(page_name).startswith("riskmitigation")


# The questionnaire gives the three de-risking areas the same title, so twelve
# distinct sections all read "De-Risking and Mitigation Measures". Reporting by
# section needs them told apart, and the page name is what carries the
# difference. This turns the page name into something readable.
AREA_WORDS = {"dataquality": "Data Quality", "fairness": "Procedural Fairness",
              "privacy": "Privacy", "consultation": "Consultations",
              "consultations": "Consultations"}


def display_name(page_name: str, title: str, phase: str) -> str:
    key = re.sub(r"[^a-z]+", "", str(page_name or "").lower())
    for stem, label in AREA_WORDS.items():
        if key.startswith(stem):
            base = f"{title} \u2014 {label}" if label not in title else title
            return f"{base} ({phase.title()})" if phase else base
    return f"{title} ({phase.title()})" if phase else title


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
                    "display_name_en": display_name(s.get("page_name"), s["section_name_en"],
                                                    s.get("mitigation_phase", "")),
                    "name_en": s["section_name_en"],
                    "name_fr": s["section_name_fr"],
                    "page_name": s.get("page_name", ""),
                    "_key": key,
                    "display_order": int(s["display_order"]),
                    "point_type": s["point_type"],
                    "mitigation_phase": s.get("mitigation_phase", ""),
                    "mitigation_area": (mitigation_area(s.get("page_name"))
                                        or (COMBINED_AREA if is_combined_mitigation_page(
                                            s.get("page_name")) else "")),
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
                "mitigation_area": (mitigation_area(s.get("page_name"))
                                    or (COMBINED_AREA if is_combined_mitigation_page(
                                        s.get("page_name")) else "")),
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

    # A question_uid holds at most one field per version. The one exception is
    # the design and implementation copies of a mitigation question, which are
    # the same question asked at a different phase; a submission only ever sees
    # one of them. Without this rule, short follow-ups with near-identical
    # wording ("Please describe", "Specify") under different parents were
    # folded into one uid, so one submission was shown the "same" question four
    # times and coverage counted it 228 times across 40 submissions.
    used = defaultdict(list)          # (version, uid) -> [phase of each field]

    def free(c, v, phase):
        taken = used.get((v, c["question_uid"]), [])
        return all(p and phase and p != phase for p in taken)

    for v in versions:
        for f in by_ver[v]:
            name = f["field_name"]
            # v0.5 left its implementation pages untagged; the page name still
            # says which phase it is, and the pairing rule depends on it.
            phase = f.get("mitigation_phase", "") or next(
                (p for p in ("implementation", "design") if p in page_key(f.get("page_name"))), "")
            sec_uid = sec_lookup.get((v, f["page_name"]))
            f_text, f_parent = signature(f, v)
            target, score, method = None, 0.0, ""

            # pass 1 — same field name, and wording has not diverged
            for c in by_name.get(name, []):
                if not free(c, v, phase):
                    continue
                s = similarity(c["canonical_text_en"], f["text_en"])
                if s >= WEAK or c["point_type"] == f["point_type"]:
                    target, score, method = c, max(s, 0.95), "field_name"
                    break

            # pass 2 — same section, near-identical wording and parent
            if target is None:
                for c in canon:
                    if c["section_uid"] != sec_uid or not free(c, v, phase):
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
                    if not free(c, v, phase):
                        continue
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
                # A question's section is where it sits now, not where it first
                # appeared. Keeping the first meant most mitigation questions
                # reported under the v0.3 combined page.
                target["section_uid"] = sec_uid
                if name not in target["field_names"].split("; "):
                    target["field_names"] += f"; {name}"
                    by_name[name].append(target)
                if score < 1.0 and method != "field_name":
                    target["reworded"] = "Y"

            used[(v, target["question_uid"])].append(phase)
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
    assign_mitigation_areas(canon, mapping)
    return canon, mapping, review


def assign_mitigation_areas(canon, mapping):
    """
    Give every mitigation question, and every row of it in every version, one of
    the four areas.

    A question's area is the area of the page it sits on in the latest version
    that places it on a named area page. The design and implementation copies of
    a question share an area, since they are the same question asked at a
    different phase. Rows on the v0.3-0.4 combined pages take their question's
    area; a question that never left those pages is marked as combined.
    """
    by_q = defaultdict(list)
    for r in mapping:
        by_q[r["question_uid"]].append(r)
    area_of = {}
    for uid, rows in by_q.items():
        named = [r for r in rows if mitigation_area(r.get("page_name"))]
        if named:
            latest = max(named, key=lambda r: version_key(r["catalog_version"]))
            area_of[uid] = mitigation_area(latest["page_name"])
        elif any(is_combined_mitigation_page(r.get("page_name")) for r in rows):
            area_of[uid] = COMBINED_AREA
    for r in mapping:
        page = r.get("page_name")
        if mitigation_area(page):
            r["mitigation_area"] = mitigation_area(page)      # where it sat in that version
        elif is_combined_mitigation_page(page):
            r["mitigation_area"] = area_of.get(r["question_uid"], COMBINED_AREA)
        else:
            r["mitigation_area"] = ""
    for c in canon:
        c["mitigation_area"] = area_of.get(c["question_uid"], "")


# --------------------------------------------------------------------------
# Registry: identifiers that do not move
# --------------------------------------------------------------------------
# Matching proposes which fields are the same question; the registry decides
# what that question is called. question_uid and section_uid used to be counters
# assigned in matching order, so every improvement to the matching renumbered
# everything after it and broke report filters (Gender Based Analysis Plus was
# 31, then 39). Now a field already in the registry keeps its identifier
# whatever the matching says, and new identifiers are only ever issued above the
# highest one in use. Where matching disagrees with the registry, the registry
# wins and the disagreement is written to registry_review.csv for a person to
# decide; changing an identifier is a deliberate edit to the registry, visible
# as a diff in git.
REGISTRY_DIR = BASE / "registry"
QUESTION_REGISTRY = REGISTRY_DIR / "question_registry.csv"
SECTION_REGISTRY = REGISTRY_DIR / "section_registry.csv"


def read_registry(path, key_cols, id_col) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return {tuple(r[k] for k in key_cols): int(r[id_col])
                for r in csv.DictReader(f) if r.get(id_col)}


def write_registry(path, key_cols, id_col, assigned: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(assigned.items(), key=lambda kv: (kv[1], version_key(kv[0][0]), kv[0][1]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(list(key_cols) + [id_col])
        for key, uid in rows:
            w.writerow(list(key) + [uid])


def settle_ids(groups, registry, conflicts=lambda uid, member, taken: False):
    """
    Turn proposed groups into lasting identifiers.

    groups: {proposed_id: [member key, ...]} in the order matching produced them.
    A registered member keeps its identifier. Unregistered members take the
    identifier most of their group's registered members hold, unless that would
    break a rule (conflicts); otherwise the group gets one new identifier above
    every one in use. Returns ({member: id}, [disagreements]).
    """
    final, review = {}, []
    next_id = max(registry.values(), default=0) + 1
    taken = defaultdict(list)
    for members in groups.values():                 # registered members first
        for m in members:
            if m in registry:
                final[m] = registry[m]
                taken[registry[m]].append(m)
    for pid, members in groups.items():
        regs = [registry[m] for m in members if m in registry]
        counts = defaultdict(int)
        for r in regs:
            counts[r] += 1
        dominant = min(counts, key=lambda r: (-counts[r], r)) if counts else None
        for m in members:
            if m in registry and dominant is not None and registry[m] != dominant:
                review.append({"member": m, "registered_id": registry[m],
                               "matching_suggests": dominant})
        fresh = None
        for m in members:
            if m in final:
                continue
            uid = dominant
            if uid is None or conflicts(uid, m, taken[uid]):
                if fresh is None:
                    fresh, next_id = next_id, next_id + 1
                uid = fresh
            final[m] = uid
            taken[uid].append(m)
    return final, review


def rebuild_questions(mapping, versions):
    """The per-question table, rebuilt from the per-version rows after ids settle."""
    by_uid = defaultdict(list)
    for r in mapping:
        by_uid[r["question_uid"]].append(r)
    canon = []
    for uid, rows in by_uid.items():
        first, last = rows[0], rows[-1]
        names = []
        for r in rows:
            if r["field_name"] not in names:
                names.append(r["field_name"])
        parent = next((r["parent_question_uid"] for r in rows if r.get("parent_question_uid")), "")
        canon.append({
            "question_uid": uid,
            "canonical_text_en": first.get("text_en", ""),
            "canonical_text_fr": first.get("text_fr", ""),
            "section_uid": last.get("section_uid", ""),
            "point_type": first.get("point_type", ""),
            "answer_type": first.get("answer_type", ""),
            "first_seen_version": min((r["catalog_version"] for r in rows), key=version_key),
            "last_seen_version": max((r["catalog_version"] for r in rows), key=version_key),
            "version_count": len(rows),
            "field_names": "; ".join(names),
            "reworded": "Y" if any(r.get("match_method") in ("text_and_parent", "text_global")
                                   and float(r.get("match_score") or 1) < 1 for r in rows) else "N",
        })
    for c in canon:
        c["status"] = "active" if c["last_seen_version"] == versions[-1] else "retired"
        c["parent_question_uid"] = next(
            (r["parent_question_uid"] for r in by_uid[c["question_uid"]]
             if r.get("parent_question_uid")), "")
        c["is_follow_up"] = "Y" if c["parent_question_uid"] else "N"
    canon.sort(key=lambda c: int(c["question_uid"]))
    assign_mitigation_areas(canon, mapping)
    return canon


def phase_of(row) -> str:
    return row.get("mitigation_phase", "") or next(
        (p for p in ("implementation", "design") if p in page_key(row.get("page_name"))), "")


def apply_registries(canon_sec, sec_map, q_map, versions):
    """Replace proposed section and question ids with registered ones."""
    # sections: one id per page, stable across runs
    s_reg = read_registry(SECTION_REGISTRY, ("catalog_version", "page_name"), "section_uid")
    s_groups = defaultdict(list)
    for r in sec_map:
        s_groups[r["section_uid"]].append((r["catalog_version"], r["page_name"]))
    s_final, s_review = settle_ids(s_groups, s_reg)
    proposed_to_final = {}
    for r in sec_map:
        key = (r["catalog_version"], r["page_name"])
        proposed_to_final.setdefault(r["section_uid"], s_final[key])
        r["section_uid"] = s_final[key]
    for c in canon_sec:
        c["section_uid"] = proposed_to_final.get(c["section_uid"], c["section_uid"])
    canon_sec.sort(key=lambda c: int(c["section_uid"]))
    sec_of = {(r["catalog_version"], r["page_name"]): r["section_uid"] for r in sec_map}
    for r in q_map:
        r["section_uid"] = sec_of.get((r["catalog_version"], r["page_name"]), r["section_uid"])

    # questions: one field per version, except a design/implementation pair
    q_reg = read_registry(QUESTION_REGISTRY, ("catalog_version", "field_name"), "question_uid")
    row_of = {(r["catalog_version"], r["field_name"]): r for r in q_map}

    def conflicts(uid, member, holders):
        v, ph = member[0], phase_of(row_of[member])
        same_version = [phase_of(row_of[h]) for h in holders if h[0] == v]
        return not all(p and ph and p != ph for p in same_version)

    q_groups = defaultdict(list)
    for r in q_map:
        q_groups[r["question_uid"]].append((r["catalog_version"], r["field_name"]))
    q_final, q_review = settle_ids(q_groups, q_reg, conflicts)
    for r in q_map:
        r["question_uid"] = q_final[(r["catalog_version"], r["field_name"])]
    uid_by_field = {(r["catalog_version"], r["field_name"]): r["question_uid"] for r in q_map}
    for r in q_map:
        p = r.get("parent_field_name")
        r["parent_question_uid"] = uid_by_field.get((r["catalog_version"], p), "") if p else ""
        r["root_question_uid"] = uid_by_field.get(
            (r["catalog_version"], r.get("root_field_name")), r["question_uid"])
    canon_q = rebuild_questions(q_map, versions)

    new_q = sum(1 for k in q_final if k not in q_reg)
    new_s = sum(1 for k in s_final if k not in s_reg)
    write_registry(QUESTION_REGISTRY, ("catalog_version", "field_name"), "question_uid", q_final)
    write_registry(SECTION_REGISTRY, ("catalog_version", "page_name"), "section_uid", s_final)
    review = ([{"kind": "question", "catalog_version": m[0], "name": m[1],
                "registered_id": x["registered_id"], "matching_suggests": x["matching_suggests"]}
               for x in q_review for m in [x["member"]]] +
              [{"kind": "section", "catalog_version": m[0], "name": m[1],
                "registered_id": x["registered_id"], "matching_suggests": x["matching_suggests"]}
               for x in s_review for m in [x["member"]]])
    return canon_sec, sec_map, canon_q, q_map, {
        "seeded": not q_reg, "new_questions": new_q, "new_sections": new_s,
        "kept_questions": len(q_final) - new_q, "kept_sections": len(s_final) - new_s,
        "review": review}


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
    canon_sec, sec_map, canon_q, q_map, reg = apply_registries(canon_sec, sec_map, q_map, versions)
    write_csv("registry_review.csv", reg["review"])

    write_csv("sections.csv", canon_sec)
    write_csv("section_versions.csv", sec_map)
    write_csv("questions.csv", canon_q)
    write_csv("question_map.csv", q_map)
    write_csv("bridge_review.csv", review)

    header("SUMMARY")
    if reg["seeded"]:
        log(f"  registry created: {len(q_map)} fields and {len(sec_map)} section pages")
        log(f"    -> {QUESTION_REGISTRY}  (commit it; identifiers are fixed from here on)")
    else:
        log(f"  registry: {reg['kept_questions']} fields kept their question_uid, "
            f"{reg['new_questions']} new; {reg['kept_sections']} section pages kept their "
            f"section_uid, {reg['new_sections']} new")
    if reg["review"]:
        log(f"  matching disagrees with the registry on {len(reg['review'])} item(s); the "
            f"registry was kept -> registry_review.csv")
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
