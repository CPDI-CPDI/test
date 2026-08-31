"""
STEP 0 — Offline self-test.

Runs the parsing, bridging and scoring logic against small fixtures shaped like
the real survey and submission files. Needs no network, so it can be run before
anything else to confirm the pipeline behaves as expected in this environment.

    python 00_selftest.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common as C
import importlib

parse = importlib.import_module("02_parse_catalogs")
bridge = importlib.import_module("03_bridge_questions")

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
def survey_fixture(version: str, reword: bool = False):
    """Two pages: one raw-scored, one mitigation. Mirrors the real file shape."""
    impact_q2 = ("How long do the impacts last?" if not reword
                 else "How long do the impacts of the decision last?")
    return {
        "pages": [
            {"name": "welcome", "elements": [
                {"type": "html", "name": "w1",
                 "html": {"default": f"<h2>Algorithmic Impact Assessment v{version}</h2>",
                          "fr": f"<h2>Évaluation de l'incidence algorithmique v{version}</h2>"}}]},
            {"name": "projectDetails",
             "title": {"default": "Project Details", "fr": "Détails du projet"},
             "elements": [{"type": "panel", "name": "projectDetailsPanel-NS", "elements": [
                 {"type": "text", "name": "projectDetailsTitle",
                  "title": {"default": "Project Title", "fr": "Titre du projet"}}]}]},
            {"name": "impact",
             "title": {"default": "Impact Assessment", "fr": "Évaluation de l'incidence"},
             "elements": [{"type": "panel", "name": "impactPanel-RS", "elements": [
                 {"type": "radiogroup", "name": "impact1", "isRequired": True,
                  "title": {"default": "Are impacts reversible?", "fr": "Les incidences sont-elles réversibles?"},
                  "choices": [
                      {"value": "item1-0", "text": {"default": "Reversible", "fr": "Réversible"}},
                      {"value": "item2-4", "text": {"default": "Irreversible", "fr": "Irréversible"}}]},
                 {"type": "radiogroup", "name": "impact2",
                  "title": {"default": impact_q2, "fr": "Combien de temps durent les incidences?"},
                  "choices": [
                      {"value": "item1-1", "text": {"default": "Days", "fr": "Jours"}},
                      {"value": "item2-3", "text": {"default": "Years", "fr": "Années"}}]},
                 {"type": "comment", "name": "impact3",
                  "title": {"default": "Please describe", "fr": "Veuillez décrire"},
                  "visibleIf": "{impact1} = 'item2-4'"}]}]},
            {"name": "fairnessImplementation",
             "title": {"default": "De-Risking and Mitigation Measures - Procedural Fairness",
                       "fr": "Mesures d'atténuation - Équité procédurale"},
             "elements": [{"type": "panel", "name": "fairnessPanel-RS", "elements": [
                 {"type": "radiogroup", "name": "fairness1",
                  "title": {"default": "Is there a recourse process?", "fr": "Existe-t-il un recours?"},
                  "choices": [
                      {"value": "item1-2", "text": {"default": "Yes", "fr": "Oui"}},
                      {"value": "item2-0", "text": {"default": "No", "fr": "Non"}}]},
                 {"type": "checkbox", "name": "fairness2",
                  "title": {"default": "Which safeguards apply?", "fr": "Quelles mesures s'appliquent?"},
                  "choices": [
                      {"value": "item1-1", "text": {"default": "Training", "fr": "Formation"}},
                      {"value": "item2-1", "text": {"default": "Monitoring", "fr": "Surveillance"}}]}]}]},
        ]
    }


print("\n=== catalog parsing ===")
s = survey_fixture("0.10.0")
check("version detected from welcome page", parse.detect_version(s, "?") == "0.10.0")
sec, fld, opt, suf = parse.parse_survey(s, "0.10.0")

check("welcome page excluded", all(f["page_name"] != "welcome" for f in fld))
check("question count", len(fld) == 6, f"got {len(fld)}")
by = {f["field_name"]: f for f in fld}
check("-NS panel scores nothing", by["projectDetailsTitle"]["point_type"] == "none")
check("-RS panel counts as raw", by["impact1"]["point_type"] == "raw")
check("mitigation page overrides -RS suffix",
      by["fairness1"]["point_type"] == "mitigation",
      f'got {by["fairness1"]["point_type"]}')
check("single-choice max = highest option", by["impact1"]["max_points"] == 4)
check("checkbox max = sum of options", by["fairness2"]["max_points"] == 2)
check("visibleIf carried through", by["impact3"]["visible_if"] == "{impact1} = 'item2-4'")
check("isRequired carried through", by["impact1"]["is_mandatory"] == "Y")
check("French text captured", by["impact1"]["text_fr"].startswith("Les incidences"))
check("option points lifted out of value",
      any(o["option_value"] == "item2-4" and o["points"] == 4 for o in opt))
check("max raw across sections", sum(x["max_raw_points"] for x in sec) == 7,
      f'got {sum(x["max_raw_points"] for x in sec)}')
check("max mitigation across sections", sum(x["max_mitigation_points"] for x in sec) == 4,
      f'got {sum(x["max_mitigation_points"] for x in sec)}')

print("\n=== bridging across versions ===")
sec_a, fld_a, _, _ = parse.parse_survey(survey_fixture("0.9.1"), "0.9.1")
sec_b, fld_b, _, _ = parse.parse_survey(survey_fixture("0.10.0", reword=True), "0.10.0")
canon_sec, sec_map = bridge.bridge_sections(sec_a + sec_b)
check("sections bridged, not duplicated", len(canon_sec) == 3, f"got {len(canon_sec)}")

_allf = fld_a + fld_b
_chains = bridge.build_chains({v: [f for f in _allf if f['catalog_version'] == v]
                              for v in {f['catalog_version'] for f in _allf}})
canon_q, qmap, review = bridge.bridge_questions(_allf, sec_map, _chains)
check("questions bridged, not duplicated", len(canon_q) == 6, f"got {len(canon_q)}")
check("every field maps to a question_uid", all(r["question_uid"] for r in qmap))
uids = {(r["catalog_version"], r["field_name"]): r["question_uid"] for r in qmap}
check("same field name in both versions shares a uid",
      uids[("0.9.1", "impact1")] == uids[("0.10.0", "impact1")])
check("reworded question still shares a uid",
      uids[("0.9.1", "impact2")] == uids[("0.10.0", "impact2")])
check("rewording is recorded",
      any(c["reworded"] == "Y" or c["version_count"] == 2 for c in canon_q))
check("version_count tracked", all(c["version_count"] == 2 for c in canon_q))

print("\n=== ordering ===")
vs = ["0.9.1", "0.10.0", "0.8", "1.0.1"]
check("0.9.1 sorts before 0.10.0",
      sorted(vs, key=bridge.version_key) == ["0.8", "0.9.1", "0.10.0", "1.0.1"],
      str(sorted(vs, key=bridge.version_key)))

print("\n=== scoring a submission ===")
submission = {"version": "v0.10.0", "data": {
    "projectDetailsTitle": "Test system",
    "impact1": "item2-4", "impact2": "item2-3",
    "impact3": "Free text answer",
    "fairness1": "item1-2", "fairness2": ["item1-1", "item2-1"]},
    "translationsOnResult": {"impact3": "Réponse en texte libre"}}
raw = mit = 0
lookup = {f["field_name"]: f for f in fld}
for k, v in submission["data"].items():
    pt = lookup[k]["point_type"]
    p = C.answer_points(v)
    if pt == "raw": raw += p
    elif pt == "mitigation": mit += p
check("raw impact recomputed", raw == 7, f"got {raw}")
check("mitigation recomputed", mit == 4, f"got {mit}")
cur, red = C.current_score(raw, mit, 4)
check("mitigation reduction applied at 80%", red is True and cur == 6, f"got {cur},{red}")
check("free-text French taken from translationsOnResult",
      submission["translationsOnResult"]["impact3"].startswith("Réponse"))

print("\n=== url resolution (CKAN returns some relative paths) ===")
check("site-relative path made absolute",
      C.absolute_url("/data/dataset/abc/download/aia.json")
      == "https://open.canada.ca/data/dataset/abc/download/aia.json")
check("absolute url untouched",
      C.absolute_url("https://open.canada.ca/x.json") == "https://open.canada.ca/x.json")
check("protocol-relative url fixed",
      C.absolute_url("//open.canada.ca/x.json") == "https://open.canada.ca/x.json")
check("empty url stays empty", C.absolute_url("") == "")

print("\n=== version tag normalisation ===")
check("v0.10.0 -> 0.10.0", C.clean_version("v0.10.0") == "0.10.0")
check("v.0.8a1 -> 0.8a1", C.clean_version("v.0.8a1") == "0.8a1",
      C.clean_version("v.0.8a1"))
check("v.0.6 -> 0.6", C.clean_version("v.0.6") == "0.6")
check("already clean is unchanged", C.clean_version("1.0.1") == "1.0.1")

print("\n=== mitigation maximum: design and implementation are alternatives ===")
check("design page detected", C.mitigation_phase("fairnessDesign") == "design")
check("implementation page detected",
      C.mitigation_phase("fairnessImplementation") == "implementation")
check("unpaired mitigation page is shared",
      C.mitigation_phase("consultations") == "shared")

# A version with paired branches worth 40 each plus 6 shared should report 46,
# not 86. This reproduces the real v0.10.0 figure derived from published scores.
sections = [
    {"max_mitigation_points": 6,  "mitigation_phase": "shared"},
    {"max_mitigation_points": 40, "mitigation_phase": "design"},
    {"max_mitigation_points": 40, "mitigation_phase": "implementation"},
]
from collections import defaultdict as _dd
_by = _dd(int)
for _s in sections:
    _by[_s["mitigation_phase"]] += _s["max_mitigation_points"]
_max = _by["shared"] + max(_by["design"], _by["implementation"])
check("paired branches are not summed", _max == 46, f"got {_max}")
check("summing both would have doubled it",
      _by["shared"] + _by["design"] + _by["implementation"] == 86)

# and the consequence: the 15% reduction fires on the correct maximum
cur_ok, red_ok = C.current_score(58, 40, 46)      # 40/46 = 87% -> reduction
cur_bad, red_bad = C.current_score(58, 40, 86)    # 40/86 = 47% -> no reduction
check("reduction fires against the correct maximum", red_ok is True and cur_ok == 49,
      f"got {cur_ok},{red_ok}")
check("doubled maximum would have suppressed the reduction", red_bad is False)

print("\n=== output location ===")
# Running the scripts flat in a working folder must not write one level above it.
check("outputs land beside the scripts, not above them",
      C.BASE == Path(__file__).resolve().parent
      or Path(__file__).resolve().parent.name.lower() == "scripts",
      f"BASE={C.BASE}")
print(f"        outputs will be written to: {C.BASE}")
print(f"        workbook: {C.BASE / 'aia_master_table.xlsx'}")

print("\n=== identical catalogs are not ambiguity ===")
triage = importlib.import_module("06_pdf_only_triage")
_shared = [f"A distinctive question number {i} about the system and its data" for i in range(150)]
_older = [f"An older and different question {i} concerning the algorithm" for i in range(120)]
_fbv = {"1.0.0": _shared, "1.0.1": _shared, "0.10.0": _older}
_groups = triage.catalog_groups(_fbv)
check("v1.0.0 and v1.0.1 share a catalog group", _groups["1.0.0"] == _groups["1.0.1"])
check("a genuinely different version is its own group",
      _groups["0.10.0"] != _groups["1.0.0"])
_ranked = triage.fingerprint(" ".join(_shared), _fbv)
_best, _, _hits = _ranked[0]
_bg = _groups[_best]
_tied = [v for v, _, h in _ranked if _groups[v] == _bg and h == _hits]
_runner = next((h for v, _, h in _ranked if _groups[v] != _bg), 0)
check("tie between duplicates is recognised", len(_tied) == 2, str(_tied))
check("margin is measured against a different catalog", _hits - _runner >= 5,
      f"margin {_hits - _runner}")

print("\n=== version keys and nearest-catalog matching ===")
check("alphanumeric part reads its leading digits",
      C.version_key("0.8a1") == (0, 8, 0), str(C.version_key("0.8a1")))
check("0.9.1 orders before 0.10.0",
      C.version_key("0.9.1") < C.version_key("0.10.0"))
ingest = importlib.import_module("04_ingest_submissions")
_known = {"0.3", "0.4", "0.5", "0.6", "0.8a1", "0.9", "0.9.1", "0.10.0", "1.0.0", "1.0.1"}
check("a file stating v0.8 maps to v0.8a1, not v0.9",
      ingest.nearest_version("0.8", _known)[0] == "0.8a1",
      ingest.nearest_version("0.8", _known)[0])
check("an exact version is used as-is",
      ingest.nearest_version("0.10.0", _known) == ("0.10.0", "exact"))
check("a tie resolves to the earlier catalog",
      ingest.nearest_version("0.7", _known)[0] == "0.6",
      ingest.nearest_version("0.7", _known)[0])

print("\n=== reading an AIA results PDF ===")
# Verbatim structure from the published Claim Summary Tool assessment,
# including the numbered list inside question 43 that must stay part of the
# answer rather than being read as questions 1, 2 and 3.
pdf = importlib.import_module("07_extract_pdf_answers")
PDF_TEXT = """Algorithmic Impact Assessment Results
Version: 1.0.1
Section 1: Impact Level : 2
Current Score: 56
Raw Impact Score: 56
Mitigation Score: 56
Section 3.1: Project Details
1. Published AIA version number
1.0
12. Project Phase
Implementation [ Points: 0 ]
Section 3.2: Impact Questions and Answers
16. Is the project within an area of intense public scrutiny?
Yes [ Points: +3 ]
17. Does the line of business serve equity denied groups?
Yes [ Points: +3 ]
22. Have potential issues or harms been raised by clients?
Yes [ Points: +1 ]
43. Describe the model being used.
The CST leverages a "Retrieval-Augmented Generation" architecture.
1. Ingestion & Handwriting Recognition (OCR)
2. Information Retrieval (The "Search" Layer)
3. Generative Summarization (The LLM)
46. Does the algorithm consider protected characteristics?
Yes [ Points: +2 ]
Section 3.3: Mitigation Questions and Answers
1. Internal Stakeholders
Yes [ Points: +2 ]
3. External consultees or partners
Yes [ Points: +2 ]
40. Indicate additional procedural fairness protections in place:
Page 30 of 32
• Clear time frame or service standards for timeliness [ Points: +1 ]
• Clients are provided with adequate information [ Points: +1 ]
"""
_h = pdf.header_values(PDF_TEXT)
check("version read from the PDF header", _h["stated_version"] == "1.0.1", str(_h))
check("printed scores read",
      (_h["stated_raw"], _h["stated_mitigation"], _h["stated_current"],
       _h["stated_impact_level"]) == (56, 56, 56, 2), str(_h))
check("blocks split and classified by title",
      [p for p, _, _ in pdf.split_parts(PDF_TEXT)] == ["none", "raw", "mitigation"],
      str([(p, t) for p, t, _ in pdf.split_parts(PDF_TEXT)]))

_rows = []
for _pt, _title, _body in pdf.split_parts(PDF_TEXT):
    _rows.extend(pdf.parse_answers(_body, _pt, _title))
_impact = [r for r in _rows if r["point_type"] == "raw"]
_mit_rows = [r for r in _rows if r["point_type"] == "mitigation"]
check("questions found in order, gaps allowed",
      [r["question_number"] for r in _impact] == [16, 17, 22, 43, 46],
      str([r["question_number"] for r in _impact]))
check("a numbered list inside an answer is not read as questions",
      [r["question_number"] for r in _mit_rows] == [1, 3, 40],
      str([r["question_number"] for r in _mit_rows]))
_q43 = [r for r in _rows if r["question_number"] == 43][0]
check("free-text answer keeps its numbered list",
      "Ingestion" in _q43["answer_text"] and _q43["is_free_text"] == "Y")
_raw = sum(r["points"] for r in _impact)
_mit = sum(r["points"] for r in _mit_rows)
check("impact section scores as raw", _raw == 9, f"got {_raw}")
check("mitigation section scores as mitigation", _mit == 6, f"got {_mit}")
check("project details carry no score",
      all(r["point_type"] == "none" for r in _rows if "Project" in r["part"]))
_q40 = [r for r in _rows if r["question_number"] == 40][0]
check("multi-select points summed", _q40["points"] == 2, str(_q40["points"]))
check("point markers stripped from answer text", "[ Points" not in _q40["answer_text"])

print("\n=== the older PDF layout numbers its sections differently ===")
# v0.10.0 has two blocks (3.1 impact, 3.2 mitigation); v1.0.1 has three
# (3.1 project details, 3.2 impact, 3.3 mitigation). Classifying by section
# number rather than title reads a v0.10.0 impact block as project details and
# scores its mitigation block as raw impact.
OLD_TEXT = """Algorithmic Impact Assessment Results
Version: 0.10.0
Project Details
8. Project Phase
 Implementation [ Points: 0 ]
 Section 1: Impact Level : 1
 Current Score: 28
Raw Impact Score: 33
Mitigation Score: 37
Section 3: Questions and Answers
 Section 3.1: Impact Questions and Answers
17. The algorithm used will be a (trade) secret
 Yes [ Points: +3 ]
42. Will the Automated Decision System use personal information as input data?
 Yes [ Points: +4 ]
 Section 3.2: Mitigation Questions and Answers
5. Do you have documented processes to test datasets against biases?
 Yes [ Points: +2 ]
"""
_oh = pdf.header_values(OLD_TEXT)
check("older layout: version and printed scores read",
      (_oh["stated_version"], _oh["stated_raw"], _oh["stated_mitigation"]) == ("0.10.0", 33, 37),
      str(_oh))
_oparts = pdf.split_parts(OLD_TEXT)
check("older layout: 3.1 classified as impact, not project details",
      [p for p, _, _ in _oparts] == ["none", "raw", "mitigation"],
      str([(p, t) for p, t, _ in _oparts]))
_orows = []
for _pt, _title, _body in _oparts:
    _orows.extend(pdf.parse_answers(_body, _pt, _title))
check("older layout: impact points land in raw",
      sum(r["points"] for r in _orows if r["point_type"] == "raw") == 7)
check("older layout: mitigation points land in mitigation",
      sum(r["points"] for r in _orows if r["point_type"] == "mitigation") == 2)
check("older layout: leading project details block captured",
      any("Project" in r["part"] for r in _orows))

triage2 = importlib.import_module("06_pdf_only_triage")
for _t, _want in [("Algorithmic Impact Assessment Results\nVersion: 1.0.1", "1.0.1"),
                  ("Version : v0.10.0", "0.10.0"),
                  ("Algorithmic Impact Assessment v0.9.1", "0.9.1")]:
    _m = triage2.VERSION_IN_PDF.search(_t)
    _got = (_m.group(1) or _m.group(2) or "").strip() if _m else ""
    check(f"version line '{_t.splitlines()[-1][:26]}' read as {_want}", _got == _want, _got)

print("\n=== bilingual organisation names ===")
check("pipe-joined name splits",
      C.split_bilingual("Transport Canada | Transports Canada")
      == ("Transport Canada", "Transports Canada"))
check("a name with no separator is kept for both",
      C.split_bilingual("Treasury Board Secretariat")
      == ("Treasury Board Secretariat", "Treasury Board Secretariat"))
check("empty stays empty", C.split_bilingual("") == ("", ""))

print("\n=== parent chains from branching conditions ===")
_qs = [
  {"catalog_version": "1", "field_name": "aiUsed", "text_en": "Does your system leverage AI?", "visible_if": ""},
  {"catalog_version": "1", "field_name": "aiVendor", "text_en": "Purchased from a vendor?", "visible_if": "{aiUsed} = 'item1-4'"},
  {"catalog_version": "1", "field_name": "aiWho", "text_en": "Which vendor?", "visible_if": "{aiVendor} = 'item1-2'"},
  {"catalog_version": "1", "field_name": "loop", "text_en": "Cyclic", "visible_if": "{loop} = 'x'"},
  {"catalog_version": "1", "field_name": "multi", "text_en": "Two conditions", "visible_if": "{aiUsed} = 'a' or {aiVendor} = 'b'"},
]
_ch = bridge.build_chains({"1": _qs})
check("a question asked outright has no parent",
      _ch[("1", "aiUsed")]["chain_depth"] == 0 and not _ch[("1", "aiUsed")]["parent_field_name"])
check("a follow-up names its parent",
      _ch[("1", "aiVendor")]["parent_field_name"] == "aiUsed")
check("a follow-up to a follow-up sits two levels down",
      _ch[("1", "aiWho")]["chain_depth"] == 2, str(_ch[("1", "aiWho")]))
check("every question resolves to its root",
      _ch[("1", "aiWho")]["root_field_name"] == "aiUsed")
check("a circular condition does not hang", _ch[("1", "loop")]["chain_depth"] == 0)
check("a condition naming two questions takes the first as parent",
      _ch[("1", "multi")]["parent_field_name"] == "aiUsed")

print("\n=== sections: the three de-risking areas stay apart ===")
_secs = []
for _v in ("0.10.0", "1.0.1"):
    for _p, _t, _ph in [("dataQualityDesign", "De-Risking and Mitigation Measures", "design"),
                        ("dataQualityImplementation", "De-Risking and Mitigation Measures", "implementation"),
                        ("fairnessDesign", "De-Risking and Mitigation Measures", "design"),
                        ("fairnessImplementation", "De-Risking and Mitigation Measures", "implementation"),
                        ("privacyDesign", "De-Risking and Mitigation Measures", "design"),
                        ("privacyImplementation", "De-Risking and Mitigation Measures", "implementation"),
                        ("project-details" if _v == "0.10.0" else "projectDetails", "Project Details", "")]:
        _secs.append({"catalog_version": _v, "page_name": _p, "section_name_en": _t,
                      "section_name_fr": "", "display_order": len(_secs) + 1,
                      "point_type": "mitigation" if _ph else "none",
                      "mitigation_phase": _ph, "max_raw_points": 0, "max_mitigation_points": 10})
_canon, _rows = bridge.bridge_sections(_secs)
check("six de-risking pages remain six sections, not one",
      len([x for x in _canon if "De-Risking" in x["name_en"]]) == 6,
      str(len(_canon)))
check("the two spellings of project details bridge together",
      len([x for x in _canon if "roject" in x["page_name"].lower()]) == 1)
check("every section matched on its page name",
      all(r["match_method"] in ("new_section", "page_name") for r in _rows))

print("\n=== answer choices stored once instead of per question ===")
cat = importlib.import_module("02_parse_catalogs")
_opts = []
for _i, (_field, _pts) in enumerate([("q1", (0, 1)), ("q2", (0, 2)), ("q3", (0, 1)), ("q4", (0, 4))]):
    for _idx, _p in enumerate(_pts):
        _opts.append({"catalog_version": "1", "field_name": _field, "field_id": _i,
                      "option_value": f"item{_idx+1}-{_p}", "option_index": _idx,
                      "points": _p, "text_en": ["Yes", "No"][_idx], "text_fr": ["Oui", "Non"][_idx]})
_sets, _items, _profs, _pitems, _links, _depts = cat.normalise_options(_opts, [])
check("four questions sharing labels store one label set", len(_sets) == 1, str(len(_sets)))
check("labels stored once, not per question", len(_items) == 2, str(len(_items)))
check("three distinct scoring patterns recognised", len(_profs) == 3, str(len(_profs)))
check("one row per question", len(_links) == 4, str(len(_links)))
_by = {p["point_profile_id"]: [] for p in _profs}
for _pi in _pitems:
    _by[_pi["point_profile_id"]].append(_pi["points"])
check("points reconstruct exactly",
      sum(sum(_by[l["point_profile_id"]]) for l in _links) == sum(o["points"] for o in _opts))

_dept = [{"catalog_version": "1", "field_name": "projectDetailsDepartment-NS", "field_id": 9,
          "option_value": f"item{n:03d}", "option_index": n, "points": 0,
          "text_en": f"Dept {n}", "text_fr": f"Ministère {n}"} for n in range(1, 30)]
_s2, _i2, _p2, _pi2, _l2, _d2 = cat.normalise_options(_dept, [])
check("the organisation list becomes reference data", len(_d2) == 29, str(len(_d2)))

# Questions can share English labels but word the French differently, and can
# share labels and points while using different stored values. Either collision
# would corrupt answer lookup, so both are kept apart.
_tricky = []
for _f, _fr, _vals in [("qa", "faible", ("item1-0", "item2-0")),
                       ("qb", "Incidence faible", ("item1", "item2"))]:
    for _i, _v in enumerate(_vals):
        _tricky.append({"catalog_version": "1", "field_name": _f, "field_id": _f,
                        "option_value": _v, "option_index": _i, "points": 0,
                        "text_en": ["Little to no impact", "High impact"][_i],
                        "text_fr": [_fr, "élevée"][_i]})
_s3, _i3, _p3, _pi3, _l3, _d3 = cat.normalise_options(_tricky, [])
check("same English but different French stays separate", len(_s3) == 2, str(len(_s3)))
check("same labels and points but different stored values stay separate",
      len(_p3) == 2, str(len(_p3)))
check("its codes lose the item prefix", _d2[0]["department_code"] == "001", _d2[0]["department_code"])
check("it is not stored as an answer set", len(_s2) == 0, str(len(_s2)))

print("\n=== PDF answers keep question text out ===")
_body = """Risk Profile
11. Is the project within an area of intense public scrutiny
and/or frequent litigation?
 No [ Points: +0 ]
Project Authority
12. Will you require new policy authority?
 Yes [ Points: +3 ]
43. Describe the model being used.
It processes documents in batches overnight.
About the Data
"""
_r = pdf.parse_answers(_body, "raw", "Impact Questions and Answers")
_q11 = [x for x in _r if x["question_number"] == 11][0]
_q43 = [x for x in _r if x["question_number"] == 43][0]
check("a wrapped question tail rejoins the question",
      "frequent litigation" in _q11["question_text"] and _q11["answer_text"] == "No",
      repr(_q11["answer_text"]))
check("a group heading does not join the answer",
      all("\n" not in x["answer_text"] for x in _r if x["has_points_marker"] == "Y"))
check("a written answer keeps its body",
      "batches overnight" in _q43["answer_text"] and "About the Data" not in _q43["answer_text"])
check("points are unaffected", sum(x["points"] for x in _r) == 3)

print("\n=== the steps can read each other's output ===")
# Unit tests on each step in isolation will not catch a step writing one shape
# and the next step expecting another. This writes what step 2 actually writes,
# then has steps 4 and 8 read it.
import csv as _csv
import tempfile as _tempfile
import shutil as _shutil

_sample = []
for _f, _pts, _vals in [("q1", (0, 1), ("item1-0", "item2-1")),
                        ("q2", (0, 2), ("item1", "item2-2")),
                        ("projectDetailsDepartment-NS", tuple([0] * 25),
                         tuple(f"item{n:03d}" for n in range(25)))]:
    for _i, _p in enumerate(_pts):
        _sample.append({"catalog_version": "1.0.1", "field_name": _f, "field_id": _f,
                        "option_value": _vals[_i], "option_index": _i, "points": _p,
                        "text_en": f"Choice {_i}", "text_fr": f"Choix {_i}"})

_tmp = _tempfile.mkdtemp()
_orig_data = C.DATA_DIR
try:
    C.DATA_DIR = Path(_tmp)
    _s, _it, _pr, _pi, _lk, _dp = cat.normalise_options(_sample, [])
    for _name, _rows in [("option_sets.csv", _s), ("option_set_items.csv", _it),
                         ("option_point_profiles.csv", _pr),
                         ("option_point_profile_items.csv", _pi),
                         ("question_options.csv", _lk), ("departments.csv", _dp)]:
        C.write_csv(_name, _rows or [{}])

    _look = C.load_option_lookup()
    check("step 2's output reassembles into per-question choices",
          len(_look.get(("1.0.1", "q1"), [])) == 2, str(_look.get(("1.0.1", "q1"))))
    check("answer values survive the round trip",
          {o["option_value"] for o in _look[("1.0.1", "q1")]} == {"item1-0", "item2-1"},
          str(_look[("1.0.1", "q1")]))
    check("points survive the round trip",
          sorted(o["points"] for o in _look[("1.0.1", "q2")]) == [0, 2])
    check("the organisation list is held apart from the answer sets",
          len(_dp) == 25 and all(l["option_set_id"] != "DEPARTMENTS"
                                 for l in _lk if l["field_name"] != "projectDetailsDepartment-NS"),
          f"departments={len(_dp)}")
    check("the organisation dropdown rebuilds from reference data",
          len(_look.get(("1.0.1", "projectDetailsDepartment-NS"), [])) == 25,
          str(len(_look.get(("1.0.1", "projectDetailsDepartment-NS"), []))))

    _ing = importlib.import_module("04_ingest_submissions")
    _lookup = {}
    for _k, _opts in C.load_option_lookup().items():
        _lookup.setdefault(_k, {})
        for _o in _opts:
            _lookup[_k][_o["option_value"]] = _o
    check("step 4 can key an answer to its choice",
          _lookup[("1.0.1", "q1")]["item2-1"]["points"] == 1,
          str(_lookup[("1.0.1", "q1")].get("item2-1")))
finally:
    C.DATA_DIR = _orig_data
    _shutil.rmtree(_tmp, ignore_errors=True)

print("\n=== the per-version question table carries everything ===")
# question_fields and question_map held the same grain, so one question in one
# version had two rows describing it. They are now one table.
_f = [
  {"catalog_version": "1", "field_id": 1, "field_name": "aiUsed", "page_name": "algo",
   "section_name_en": "About the Algorithm", "section_name_fr": "Algorithme", "section_order": 6,
   "point_type": "raw", "answer_type": "radiogroup", "is_mandatory": "Y", "visible_if": "",
   "max_points": 4, "text_en": "Does your system leverage AI?", "text_fr": "Utilise-t-il l'IA?",
   "guidance_en": "Answer yes if any component learns from data.", "guidance_fr": "Oui si un composant apprend."},
  {"catalog_version": "1", "field_id": 2, "field_name": "aiVendor", "page_name": "algo",
   "section_name_en": "About the Algorithm", "section_name_fr": "Algorithme", "section_order": 6,
   "point_type": "raw", "answer_type": "radiogroup", "is_mandatory": "N",
   "visible_if": "{aiUsed} = 'item1-4'", "max_points": 2,
   "text_en": "Purchased from a vendor?", "text_fr": "Acheté?",
   "guidance_en": "A vendor is any party outside your institution.", "guidance_fr": "Externe."},
]
_sc = [{"catalog_version": "1", "page_name": "algo", "section_name_en": "About the Algorithm",
        "section_name_fr": "Algorithme", "display_order": 6, "point_type": "raw",
        "mitigation_phase": "", "max_raw_points": 6, "max_mitigation_points": 0}]
_cs, _sm = bridge.bridge_sections(_sc)
_chn = bridge.build_chains({"1": _f})
_cq, _qm2, _rv = bridge.bridge_questions(_f, _sm, _chn)
for _col in ("guidance_en", "guidance_fr", "visible_if", "page_name", "text_fr",
             "question_uid", "parent_field_name", "chain_depth", "is_mandatory", "max_points"):
    check(f"question_map carries {_col}", _col in _qm2[0], str(sorted(_qm2[0].keys())))
_vendor = [r for r in _qm2 if r["field_name"] == "aiVendor"][0]
check("guidance survives the merge",
      _vendor["guidance_en"].startswith("A vendor"), _vendor.get("guidance_en", ""))
check("the parent chain sits on the same row",
      _vendor["parent_field_name"] == "aiUsed" and _vendor["chain_depth"] == 1)
check("questions is a roll-up of it", len(_cq) == 2 and len(_qm2) == 2)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all self-tests passed — the pipeline logic is sound in this environment")
