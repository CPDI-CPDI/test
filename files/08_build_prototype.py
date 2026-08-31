"""
STEP 8 — Generate the prototype questionnaire from the real catalog.

Builds a single self-contained HTML file rendered entirely from the catalog
tables. No question, option, point value or piece of guidance is written into
the page by hand: change the catalog and rerun this step, and the questionnaire
changes with it. That is the whole argument the design document makes about the
questions table being the contract between policy and technology, demonstrated
rather than asserted.

Three things the real catalog forces that sample data did not:

  * Mitigation pages come in Design and Implementation pairs. An assessment
    answers one set according to its project phase, so the page list is filtered
    by the phase answer rather than showing all 17 pages at once.

  * Branching is real. `visible_if` conditions decide whether a follow-up
    appears. Conditions that cannot be parsed leave the question visible, since
    wrongly hiding a question is worse than wrongly showing one.

  * Only one of 295 questions carries the mandatory flag, so readiness cannot
    rely on it. Completeness is measured against every visible scored question
    instead, with written follow-ups reported separately.

Usage
    python 08_build_prototype.py              # latest version in the catalog
    python 08_build_prototype.py 0.10.0       # a specific version

Output
    aia_prototype_<version>.html
"""
from __future__ import annotations
import csv, json, re, sys
from collections import defaultdict
from common import (DATA_DIR, BASE, read_csv, version_key, load_option_lookup,
                    log, header)

PHASE_FIELD_HINT = "phase"
DESIGN_SUFFIX = "design"
IMPL_SUFFIX = "implementation"

# Mitigation pages are named consultationDesign / consultationImplementation and
# so on. Where page_name is unavailable the prefix of the field name carries the
# same information.
PAGE_FROM_FIELD = re.compile(
    r"^(consultation|dataQuality|fairness|privacy)(Design|Implementation)", re.I)


def infer_page(row: dict) -> str:
    page = (row.get("page_name") or "").strip()
    if page:
        return page
    m = PAGE_FROM_FIELD.match(row.get("field_name") or "")
    if m:
        return m.group(1) + m.group(2).capitalize()
    return row.get("section_name_en") or "other"


def phase_branch(page: str) -> str:
    low = page.lower()
    if low.endswith(DESIGN_SUFFIX):
        return "design"
    if low.endswith(IMPL_SUFFIX):
        return "implementation"
    return ""


def build_payload(version: str):
    fields = [r for r in read_csv("question_map.csv")
              if str(r["catalog_version"]) == version]
    if not fields:
        raise SystemExit(f"no questions found for version {version}")
    options = defaultdict(list)
    for (ver, field), opts in load_option_lookup().items():
        if ver == version:
            options[field] = opts
    versions = {r["catalog_version"]: r for r in read_csv("catalog_versions.csv")}
    vinfo = versions.get(version, {})

    # ---- pages, in questionnaire order ----
    order, pages = {}, {}
    for f in fields:
        page = infer_page(f)
        if page not in pages:
            pages[page] = {
                "id": page,
                "en": f.get("section_name_en") or page,
                "fr": f.get("section_name_fr") or page,
                "type": f.get("point_type") or "none",
                "branch": phase_branch(page),
                "questions": [],
            }
            order[page] = int(f.get("section_order") or len(order) + 1)

    phase_field = next((f["field_name"] for f in fields
                        if PHASE_FIELD_HINT in (f["field_name"] or "").lower()), "")

    for f in fields:
        page = infer_page(f)
        opts = sorted(options.get(f["field_name"], []),
                      key=lambda o: int(o.get("option_index") or 0))
        pages[page]["questions"].append({
            "f": f["field_name"],
            "uid": f.get("question_uid", ""),
            "t": f.get("answer_type") or "text",
            "p": f.get("point_type") or "none",
            "req": 1 if str(f.get("is_mandatory")) == "Y" else 0,
            "max": int(float(f.get("max_points") or 0)),
            "vif": (f.get("visible_if") or "").strip(),
            "en": f.get("text_en") or f["field_name"],
            "fr": f.get("text_fr") or "",
            "gen": f.get("guidance_en") or "",
            "gfr": f.get("guidance_fr") or "",
            "o": [{"v": o["option_value"], "p": int(float(o.get("points") or 0)),
                   "en": o.get("text_en") or o["option_value"],
                   "fr": o.get("text_fr") or ""} for o in opts],
        })

    ordered = [pages[p] for p in sorted(pages, key=lambda p: order[p])]
    payload = {
        "version": version,
        "maxRaw": int(float(vinfo.get("max_raw") or 0)),
        "maxMit": int(float(vinfo.get("max_mitigation") or 0)),
        "threshold": float(vinfo.get("mitigation_threshold") or 0),
        "phaseField": phase_field,
        "pages": ordered,
    }
    return payload, fields


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return HTML_TEMPLATE.replace("/*__CATALOG__*/", data)


def main():
    header("STEP 8 — Generating the prototype from the catalog")
    versions = {r["catalog_version"] for r in read_csv("catalog_versions.csv")}
    version = sys.argv[1] if len(sys.argv) > 1 else max(versions, key=version_key)
    if version not in versions:
        raise SystemExit(f"unknown version {version}. Available: "
                         + ", ".join(sorted(versions, key=version_key)))
    log(f"  building from catalog version {version}")

    payload, fields = build_payload(version)
    scored = sum(1 for p in payload["pages"] for q in p["questions"] if q["p"] != "none")
    branched = sum(1 for p in payload["pages"] for q in p["questions"] if q["vif"])
    guided = sum(1 for p in payload["pages"] for q in p["questions"] if q["gen"])
    pairs = sorted({p["id"] for p in payload["pages"] if p["branch"]})

    html = render(payload)
    out = BASE / f"aia_prototype_{version}.html"
    out.write_text(html, encoding="utf-8")

    log(f"  pages                  : {len(payload['pages'])}")
    log(f"  questions              : {sum(len(p['questions']) for p in payload['pages'])}")
    log(f"  scored questions       : {scored}")
    log(f"  with branching rules   : {branched}")
    log(f"  with guidance text     : {guided}")
    log(f"  maximum raw / mitigation: {payload['maxRaw']} / {payload['maxMit']}")
    log(f"  phase question         : {payload['phaseField'] or 'not found'}")
    if pairs:
        log(f"  design/implementation pages: {', '.join(pairs)}")
    log(f"\n  wrote {out}  ({len(html)//1024} KB)")
    if not guided:
        log("\n  No guidance text in this catalog version. The questionnaire will")
        log("  render without the 'why this matters' panels.")


# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Algorithmic Impact Assessment - Canada.ca</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700&family=Noto+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
/* ------------------------------------------------------------------
   Canada.ca design system — mandatory styles
   Typography  : Lato for headings, Noto Sans for body.
                 Desktop H1 41 / H2 39 / H3 29 / H4 27, body 20.
                 Small screens H1 37 / H2 35 / H3 26 / H4 22, body 18.
   Colour      : background #FFF, text #333, accent #26374A,
                 links #284162, hover #0535d2, visited #7834bc,
                 required or error #d3080c, sub-footer #F8F8F8.
   Page title  : red bar beneath the H1, #A62A1E, 72px wide, 6px thick,
                 left aligned, 0.2em below.
   Source: design.canada.ca/styles/typography and /styles/colours
   ------------------------------------------------------------------ */
:root{
  --gc-text:#333333;
  --gc-accent:#26374A;
  --gc-link:#284162;
  --gc-link-hover:#0535d2;
  --gc-link-visited:#7834bc;
  --gc-error:#d3080c;
  --gc-red-bar:#A62A1E;
  --gc-footer:#F8F8F8;
  --gc-rule:#CFD1D5;
  --gc-rule-soft:#E4E6E8;
  --gc-pale:#EEF1F4;
  --gc-ok:#278400;
  --gc-ok-pale:#E7F3E4;
  --gc-warn:#8A6D1D;
  --gc-warn-pale:#F5EFE0;
  --sans:'Noto Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  --head:'Lato',Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:#FFFFFF;color:var(--gc-text);font-family:var(--sans);font-size:20px;line-height:1.6}
h1,h2,h3,h4{font-family:var(--head);font-weight:700;margin:0;color:var(--gc-text)}
h1{font-size:41px;line-height:1.2}
h2{font-size:29px;line-height:1.25}
h3{font-size:24px;line-height:1.3}
a{color:var(--gc-link)}
a:hover,a:focus{color:var(--gc-link-hover)}
a:visited{color:var(--gc-link-visited)}
button{font-family:inherit}
:focus-visible{outline:3px solid var(--gc-link-hover);outline-offset:2px}
.tnum{font-variant-numeric:tabular-nums}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* ---------- GC header ---------- */
.gc-header{border-bottom:1px solid var(--gc-rule);padding:16px 0 12px}
.shell{max-width:1140px;margin:0 auto;padding:0 24px}
.gc-top{display:flex;align-items:flex-start;gap:24px;flex-wrap:wrap}
/* The Government of Canada signature and Canada wordmark are protected under
   the Federal Identity Program. They are not reproduced here: replace this
   block with the official asset from the GCWeb theme
   (GCWeb/assets/sig-blk-en.svg, 40px height) before any real use. */
.fip{
  border:1px dashed var(--gc-rule);background:var(--gc-pale);
  height:40px;min-width:280px;display:flex;align-items:center;justify-content:center;
  font-size:12px;color:#5A5A5A;font-family:var(--sans);padding:0 12px;text-align:center;
}
.gc-top-right{margin-left:auto;display:flex;align-items:center;gap:16px}
.lang-toggle button{
  background:none;border:0;color:var(--gc-link);font-size:16px;
  text-decoration:underline;cursor:pointer;padding:4px 6px;font-family:var(--sans);
}
.lang-toggle button[aria-pressed="true"]{font-weight:700;text-decoration:none;color:var(--gc-text);cursor:default}
.proto-tag{
  font-size:14px;font-weight:700;background:var(--gc-warn-pale);color:var(--gc-warn);
  padding:3px 10px;border:1px solid var(--gc-warn);
}
.app-band{background:var(--gc-accent);color:#fff;padding:12px 0}
.app-band .shell{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.app-band .name{font-family:var(--head);font-size:22px;font-weight:700}
.app-band .meta{margin-left:auto;font-size:15px;opacity:.85;display:flex;gap:18px;flex-wrap:wrap}
.app-band .meta b{font-weight:600}

/* ---------- page title with the mandatory red bar ---------- */
.page-title{margin:32px 0 24px}
.page-title h1::after{
  content:"";display:block;width:72px;height:6px;background:var(--gc-red-bar);margin-top:.2em;
}
.page-title p{margin:20px 0 0;max-width:38em}

.wrap{max-width:1140px;margin:0 auto;padding:0 24px 80px}
.view{display:none}.view.on{display:block}

/* ---------- buttons ---------- */
.btn{
  font-size:18px;padding:8px 16px;border:2px solid var(--gc-link);
  background:#fff;color:var(--gc-link);cursor:pointer;border-radius:4px;font-weight:600;
}
.btn:hover{background:var(--gc-pale)}
.btn-primary{background:var(--gc-link);color:#fff}
.btn-primary:hover{background:#1c3050}
.btn-primary:disabled{background:#9A9EA3;border-color:#9A9EA3;cursor:not-allowed}
.btn-sm{font-size:16px;padding:5px 12px;border-width:1px}

/* ---------- tracker ---------- */
table.tracker{width:100%;border-collapse:collapse;font-size:18px}
.tracker caption{text-align:left;font-size:16px;color:#5A5A5A;padding-bottom:8px}
.tracker th{background:var(--gc-accent);color:#fff;text-align:left;padding:10px 14px;font-size:16px;font-weight:600}
.tracker th.num,.tracker td.num{text-align:right}
.tracker td{padding:12px 14px;border-bottom:1px solid var(--gc-rule-soft)}
.tracker tbody tr:nth-child(even){background:#FAFAFA}
.tracker tbody tr.clickable:hover{background:var(--gc-pale);cursor:pointer}
.tracker tfoot td{border-top:2px solid var(--gc-accent);font-weight:700;background:#F4F5F6}
.sec-link{color:var(--gc-link);text-decoration:underline}
.status{font-size:16px;font-weight:600;white-space:nowrap}
.status.done{color:var(--gc-ok)}
.status.part{color:var(--gc-warn)}
.status.none{color:#5A5A5A;font-weight:400}
.na{color:#767676}

/* ---------- readiness ---------- */
.readiness{margin-top:36px;border:2px solid var(--gc-rule);padding:24px}
.readiness h2{margin-bottom:8px}
.readiness>p{margin:0 0 18px;max-width:38em;font-size:18px}
.alert{padding:14px 18px;border-left:6px solid;margin-bottom:18px;font-size:18px}
.alert.ok{border-color:var(--gc-ok);background:var(--gc-ok-pale)}
.alert.bad{border-color:var(--gc-error);background:#F9E5E5;color:#8B0A0D;font-weight:600}
.alert.warn{border-color:var(--gc-warn);background:var(--gc-warn-pale)}
.missing-list{margin:0 0 18px;padding:0;list-style:none;max-height:320px;overflow-y:auto;border:1px solid var(--gc-rule-soft)}
.missing-list li{padding:10px 14px;border-bottom:1px solid var(--gc-rule-soft);display:flex;gap:14px;align-items:baseline;font-size:17px}
.missing-list li:last-child{border-bottom:0}
.missing-list .go{margin-left:auto;background:none;border:0;color:var(--gc-link);text-decoration:underline;cursor:pointer;font-size:16px;white-space:nowrap;font-family:var(--sans)}
.export-row{display:flex;gap:12px;flex-wrap:wrap}
.note{font-size:16px;color:#5A5A5A;margin:14px 0 0;max-width:42em}

/* ---------- section layout ---------- */
.cols{display:grid;grid-template-columns:250px minmax(0,1fr) 260px;gap:32px;align-items:start;margin-top:28px}
.rail{position:sticky;top:20px;max-height:calc(100vh - 40px);overflow-y:auto}
.rail h2{font-family:var(--sans);font-size:15px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#5A5A5A;margin-bottom:10px}
.nav-list{list-style:none;margin:0;padding:0}
.nav-list button{
  width:100%;text-align:left;background:none;border:0;border-left:4px solid transparent;
  padding:8px 10px;font-size:16px;color:var(--gc-link);cursor:pointer;
  display:flex;gap:9px;align-items:baseline;line-height:1.35;font-family:var(--sans);
}
.nav-list button:hover{background:var(--gc-pale)}
.nav-list button[aria-current="true"]{border-left-color:var(--gc-accent);background:var(--gc-pale);color:var(--gc-text);font-weight:700}
.tick{font-size:14px;width:14px;flex:none}
.tick.done{color:var(--gc-ok)}
.tick.part{color:var(--gc-warn)}
.tick.todo{color:#9A9EA3}

/* ---------- questions ---------- */
.qhead{margin-bottom:24px;padding-bottom:16px;border-bottom:2px solid var(--gc-rule)}
.qhead .step{font-size:16px;color:#5A5A5A;margin:0 0 6px}
.q{border:1px solid var(--gc-rule);padding:22px;margin-bottom:18px;scroll-margin-top:20px}
.q.flagged{border-color:var(--gc-error);border-width:2px;background:#FDF6F6}
.q legend,.q .q-text{font-size:20px;font-weight:600;line-height:1.45;padding:0;margin:0 0 6px}
.q fieldset{border:0;padding:0;margin:0}
.tags{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.tag{font-size:14px;font-weight:600;padding:2px 8px;border:1px solid}
.tag.req{color:var(--gc-error);border-color:var(--gc-error)}
.tag.pts{color:var(--gc-accent);border-color:var(--gc-accent)}
.tag.mit{color:var(--gc-ok);border-color:var(--gc-ok)}
.tag.cond{color:#5A5A5A;border-color:#9A9EA3}
.opts{display:flex;flex-direction:column;gap:2px}
.opt{display:flex;align-items:flex-start;gap:12px;padding:10px 12px;cursor:pointer;font-size:18px;line-height:1.45;border:1px solid transparent}
.opt:hover{background:var(--gc-pale)}
.opt.sel{background:var(--gc-pale);border-color:var(--gc-accent)}
.opt input{margin:5px 0 0;width:20px;height:20px;accent-color:var(--gc-accent);flex:none}
.opt-pts{margin-left:auto;font-size:15px;color:#5A5A5A;white-space:nowrap;padding-top:2px}
.opt.sel .opt-pts{color:var(--gc-accent);font-weight:700}
textarea,input[type=text],select{
  width:100%;border:2px solid #6F6F6F;padding:8px 10px;font-family:var(--sans);
  font-size:18px;line-height:1.5;color:var(--gc-text);background:#fff;border-radius:0;
}
textarea{resize:vertical;min-height:110px}
textarea:focus,input[type=text]:focus,select:focus{border-color:var(--gc-link-hover)}
details.guidance{margin-top:14px}
details.guidance summary{font-size:17px;color:var(--gc-link);cursor:pointer;text-decoration:underline}
details.guidance p{margin:10px 0 0;font-size:17px;line-height:1.55;background:var(--gc-pale);padding:14px 16px;border-left:4px solid var(--gc-accent);max-width:40em}
.pager{display:flex;gap:14px;margin-top:28px;padding-top:22px;border-top:1px solid var(--gc-rule)}
.pager .sp{flex:1}

/* ---------- score panel ---------- */
.score{border:1px solid var(--gc-rule);padding:18px}
.score h2{font-family:var(--sans);font-size:15px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#5A5A5A;margin-bottom:12px}
.score .pct{font-family:var(--head);font-size:41px;font-weight:700;line-height:1;color:var(--gc-accent)}
.score .lvl{font-size:16px;color:#5A5A5A;margin:6px 0 16px;line-height:1.4}
.ladder{display:flex;flex-direction:column-reverse;gap:3px;margin-bottom:16px}
.rung{position:relative;height:34px;background:#EDEEF0;display:flex;align-items:center;padding:0 10px;font-size:15px}
.rung .fill{position:absolute;left:0;top:0;bottom:0;background:#D4DAE1;transition:width .4s}
.rung .lbl,.rung .rng{position:relative;z-index:1}
.rung .lbl{font-weight:600;color:#4A4A4A}
.rung .rng{margin-left:auto;font-size:14px;color:#6F6F6F}
.rung.on{background:var(--gc-accent)}
.rung.on .fill{background:rgba(255,255,255,.18)}
.rung.on .lbl,.rung.on .rng{color:#fff}
.split{border-top:1px solid var(--gc-rule-soft);padding-top:14px;font-size:16px}
.split .row{display:flex;margin-bottom:8px}
.split .row b{margin-left:auto;font-size:18px}
.bar{height:6px;background:#EDEEF0;overflow:hidden}
.bar i{display:block;height:100%;background:var(--gc-ok);transition:width .4s}
.remain{margin-top:14px;padding-top:12px;border-top:1px solid var(--gc-rule-soft);font-size:16px;line-height:1.45}
.remain b{color:var(--gc-error)}
.remain.clear b{color:var(--gc-ok)}

/* ---------- GC footer ---------- */
.gc-footer{background:var(--gc-footer);border-top:1px solid var(--gc-rule);margin-top:60px;padding:20px 0}
.gc-footer .shell{display:flex;align-items:center;gap:24px;flex-wrap:wrap;font-size:16px}
.gc-footer a{color:var(--gc-link);font-size:16px}
.wordmark{
  margin-left:auto;border:1px dashed var(--gc-rule);height:40px;min-width:140px;
  display:flex;align-items:center;justify-content:center;font-size:12px;color:#5A5A5A;padding:0 10px;
}
.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(10px);
  background:var(--gc-accent);color:#fff;padding:12px 22px;font-size:17px;
  opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:60;
}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

@media (max-width:1080px){
  .cols{grid-template-columns:1fr}
  .rail{position:static;max-height:none}
  .nav-list{display:flex;flex-wrap:wrap}
  .nav-list button{width:auto}
}
@media (max-width:768px){
  body{font-size:18px}
  h1{font-size:37px}h2{font-size:26px}h3{font-size:22px}
  .q legend,.q .q-text{font-size:18px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>

<header class="gc-header">
  <div class="shell gc-top">
    <div class="fip">Government of Canada signature<br>(official FIP asset goes here)</div>
    <div class="gc-top-right">
      <span class="proto-tag">Prototype</span>
      <div class="lang-toggle" role="group" aria-label="Language selection">
        <button id="langEn" aria-pressed="true" onclick="setLang('en')">English</button>
        <button id="langFr" aria-pressed="false" onclick="setLang('fr')">Français</button>
      </div>
    </div>
  </div>
</header>
<div class="app-band">
  <div class="shell">
    <span class="name" id="appName"></span>
    <span class="meta">
      <span id="dbDraft"></span><span id="dbCatalog"></span><span id="dbPhase"></span>
    </span>
  </div>
</div>

<div class="wrap">
  <section class="view on" id="viewOverview">
    <div class="page-title">
      <h1 id="ovTitle"></h1>
      <p id="ovLede"></p>
    </div>
    <table class="tracker">
      <caption id="ovCaption"></caption>
      <thead><tr>
        <th id="thSection"></th><th id="thStatus"></th>
        <th class="num" id="thAnswered"></th><th class="num" id="thRaw"></th><th class="num" id="thMit"></th>
      </tr></thead>
      <tbody id="trackerBody"></tbody><tfoot id="trackerFoot"></tfoot>
    </table>
    <div style="margin-top:20px;display:flex;gap:12px;flex-wrap:wrap">
      <button class="btn btn-sm" id="btnSave" onclick="saveJson()"></button>
      <button class="btn btn-sm" id="btnLoad" onclick="document.getElementById('fileIn').click()"></button>
      <input type="file" id="fileIn" accept=".json" style="display:none" onchange="loadJson(event)">
    </div>
    <div class="readiness">
      <h2 id="rdTitle"></h2><p id="rdLede"></p>
      <div id="rdResult"></div>
      <div class="export-row"><button class="btn btn-primary" id="btnExport" onclick="exportFinal()"></button></div>
      <p class="note" id="rdNote"></p>
    </div>
  </section>

  <section class="view" id="viewSection">
    <div class="cols">
      <nav class="rail" aria-label="Sections">
        <h2 id="navTitle"></h2>
        <ul class="nav-list" id="navList"></ul>
        <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--gc-rule)">
          <button class="btn btn-sm" style="width:100%" id="btnBack" onclick="goOverview()"></button>
        </div>
      </nav>
      <main>
        <div class="qhead">
          <p class="step" id="secStep"></p>
          <h2 id="secTitle"></h2>
        </div>
        <div id="qList"></div>
        <div class="pager">
          <button class="btn" id="btnPrev" onclick="stepSection(-1)"></button>
          <span class="sp"></span>
          <button class="btn btn-primary" id="btnNext" onclick="stepSection(1)"></button>
        </div>
      </main>
      <aside class="score" aria-label="Score">
        <h2 id="scTitle"></h2>
        <div class="pct tnum" id="scPct">0%</div>
        <p class="lvl" id="scLvl"></p>
        <div class="ladder" id="ladder"></div>
        <div class="split">
          <div class="row"><span id="scRawK"></span><b class="tnum" id="scRawV">0</b></div>
          <div class="row"><span id="scMitK"></span><b class="tnum" id="scMitV">0</b></div>
          <div class="bar"><i id="scBar" style="width:0%"></i></div>
          <p class="note" style="margin:8px 0 0;font-size:15px" id="scMitNote"></p>
        </div>
        <div class="remain" id="scRemain"></div>
      </aside>
    </div>
  </section>
</div>

<footer class="gc-footer">
  <div class="shell">
    <span id="ftNote"></span>
    <span class="wordmark">Canada wordmark</span>
  </div>
</footer>
<div class="toast" id="toast"></div>

<script>
const CATALOG = /*__CATALOG__*/;

const LEVELS=[{lo:0,hi:25,en:"Level I",fr:"Niveau I",dEn:"Little to no impact",dFr:"Incidence faible ou nulle"},
{lo:26,hi:50,en:"Level II",fr:"Niveau II",dEn:"Moderate impact",dFr:"Incidence modérée"},
{lo:51,hi:75,en:"Level III",fr:"Niveau III",dEn:"High impact",dFr:"Incidence élevée"},
{lo:76,hi:100,en:"Level IV",fr:"Niveau IV",dEn:"Very high impact",dFr:"Incidence très élevée"}];

const T={en:{
 app:"Algorithmic Impact Assessment",
 ovTitle:"Your assessment at a glance",
 ovLede:"Sections can be completed in any order. Save your progress as a file at any point to share it with colleagues or continue later.",
 ovCaption:"Progress and score by section",
 thSection:"Section",thStatus:"Status",thAnswered:"Answered",thRaw:"Raw impact",thMit:"Mitigation",
 total:"Total",complete:"Complete",partial:"In progress",notStarted:"Not started",na:"Not scored",
 rdTitle:"Readiness check",
 rdLede:"Every scored question that applies to this system needs an answer before the assessment can be exported.",
 rdOk:"Ready to export. Every scored question has an answer.",
 rdMissing:n=>`${n} scored ${n===1?"question":"questions"} still to answer`,
 rdSoft:n=>`${n} written ${n===1?"answer is":"answers are"} still blank. These carry no score, but they are what makes the published assessment readable.`,
 exportBtn:"Export assessment",
 rdNote:"Export produces the structured file. In the full service this is sent to the AIA inbox, and a stamped copy is returned to identify this system in future updates.",
 navTitle:"Sections",back:"Back to overview",prev:"Previous",next:"Next section",finish:"Review and export",
 scTitle:"Current score",scRawK:"Raw impact",scMitK:"Mitigation",
 mitShort:(n,t)=>`${n} more points needed to reach ${t} and apply the 15% reduction`,
 mitMet:"Threshold met. A 15% reduction has been applied.",
 remain:n=>n===0?"All scored questions answered":`scored ${n===1?"question":"questions"} remaining`,
 required:"Required",pointsRaw:"Scored",pointsMit:"Mitigation",conditional:"Shown conditionally",
 guidance:"Why this matters",placeholder:"Enter your answer",choose:"Select one",
 draft:"Draft",catalog:"Question set",phase:"Phase",save:"Save progress",load:"Upload a saved file",
 step:(a,b)=>`Section ${a} of ${b}`,
 tSaved:"Progress saved to your downloads",tLoaded:"Progress restored",
 tBad:"That file could not be read",tBlocked:"Answer the scored questions first",
 tExported:"Assessment exported",
 footer:"This is a prototype. It is not an official Government of Canada service."},
fr:{
 app:"Évaluation de l'incidence algorithmique",
 ovTitle:"Votre évaluation en un coup d'œil",
 ovLede:"Les sections peuvent être remplies dans n'importe quel ordre. Enregistrez votre progression à tout moment pour la partager ou la reprendre plus tard.",
 ovCaption:"Progression et score par section",
 thSection:"Section",thStatus:"État",thAnswered:"Répondu",thRaw:"Incidence brute",thMit:"Atténuation",
 total:"Total",complete:"Terminée",partial:"En cours",notStarted:"Non commencée",na:"Non cotée",
 rdTitle:"Vérification de l'état de préparation",
 rdLede:"Chaque question cotée applicable à ce système doit être remplie avant l'exportation.",
 rdOk:"Prêt à exporter. Toutes les questions cotées ont une réponse.",
 rdMissing:n=>`${n} question${n===1?"":"s"} cotée${n===1?"":"s"} à remplir`,
 rdSoft:n=>`${n} réponse${n===1?"":"s"} écrite${n===1?"":"s"} encore vide${n===1?"":"s"}. Elles ne sont pas cotées, mais ce sont elles qui rendent l'évaluation publiée lisible.`,
 exportBtn:"Exporter l'évaluation",
 rdNote:"L'exportation produit le fichier structuré. Dans le service complet, il est envoyé à la boîte de réception de l'EIA et une copie estampillée est retournée.",
 navTitle:"Sections",back:"Retour à l'aperçu",prev:"Précédent",next:"Section suivante",finish:"Réviser et exporter",
 scTitle:"Score actuel",scRawK:"Incidence brute",scMitK:"Atténuation",
 mitShort:(n,t)=>`encore ${n} points pour atteindre ${t} et appliquer la réduction de 15 %`,
 mitMet:"Seuil atteint. Une réduction de 15 % a été appliquée.",
 remain:n=>n===0?"Toutes les questions cotées sont remplies":`question${n===1?"":"s"} cotée${n===1?"":"s"} restante${n===1?"":"s"}`,
 required:"Obligatoire",pointsRaw:"Cotée",pointsMit:"Atténuation",conditional:"Affichée sous condition",
 guidance:"Pourquoi cette question",placeholder:"Saisissez votre réponse",choose:"Sélectionnez",
 draft:"Brouillon",catalog:"Jeu de questions",phase:"Étape",save:"Enregistrer",load:"Téléverser un fichier",
 step:(a,b)=>`Section ${a} sur ${b}`,
 tSaved:"Progression enregistrée",tLoaded:"Progression restaurée",
 tBad:"Ce fichier n'a pas pu être lu",tBlocked:"Remplissez d'abord les questions cotées",
 tExported:"Évaluation exportée",
 footer:"Ceci est un prototype. Il ne s'agit pas d'un service officiel du gouvernement du Canada."}};

let lang="en", answers={}, currentPage=CATALOG.pages[0].id;
let draftId="D-"+Math.random().toString(36).slice(2,8).toUpperCase();
const t=()=>T[lang], L=o=>lang==="en"?(o.en||o.fr||""):(o.fr||o.en||"");
const $=id=>document.getElementById(id);

/* ---------- branching ----------
   A condition that cannot be parsed leaves the question visible: wrongly
   hiding a question is the worse failure. */
function valueOf(f){const a=answers[f];return a===undefined?"":a;}
function testAtom(s){
  s=s.trim();
  let m=s.match(/^\{([^}]+)\}\s*(=|<>|!=)\s*'([^']*)'$/);
  if(m){const v=valueOf(m[1]);const hit=Array.isArray(v)?v.includes(m[3]):String(v)===m[3];
        return m[2]==="="?hit:!hit;}
  m=s.match(/^\{([^}]+)\}\s+(not\s*)?contains\s+'([^']*)'$/i);
  if(m){const v=valueOf(m[1]);const hit=Array.isArray(v)?v.includes(m[3]):String(v).includes(m[3]);
        return m[2]?!hit:hit;}
  m=s.match(/^\{([^}]+)\}\s+(not)?empty$/i);
  if(m){const v=valueOf(m[1]);const e=v===""||v==null||(Array.isArray(v)&&!v.length);
        return m[2]?!e:e;}
  return null;
}
function visible(q){
  if(!q.vif) return true;
  const parts=q.vif.replace(/[()]/g," ").split(/\s+or\s+/i);
  let unknown=false;
  for(const part of parts){
    let all=true;
    for(const a of part.split(/\s+and\s+/i)){
      const r=testAtom(a);
      if(r===null){unknown=true;all=false;break;}
      if(!r){all=false;break;}
    }
    if(all) return true;
  }
  return unknown;
}
/* Mitigation pages come in Design and Implementation pairs; one branch applies. */
function phase(){
  const v=CATALOG.phaseField?valueOf(CATALOG.phaseField):"";
  const q=CATALOG.pages.flatMap(p=>p.questions).find(x=>x.f===CATALOG.phaseField);
  if(!q||!v) return "implementation";
  const o=q.o.find(x=>x.v===v);
  const label=((o&&o.en)||"").toLowerCase();
  return /design|concept|plan/.test(label)?"design":"implementation";
}
const pages=()=>CATALOG.pages.filter(p=>!p.branch||p.branch===phase());
const qsOf=p=>p.questions.filter(visible);

function pointsFor(q){
  const v=valueOf(q.f);
  if(v===""||v==null) return 0;
  const vals=Array.isArray(v)?v:[v];
  return q.o.filter(o=>vals.includes(o.v)).reduce((a,o)=>a+o.p,0);
}
function scores(){
  let raw=0,mit=0;
  pages().forEach(p=>qsOf(p).forEach(q=>{
    const v=pointsFor(q);
    if(q.p==="raw")raw+=v; else if(q.p==="mitigation")mit+=v;}));
  const reduced=CATALOG.maxMit>0&&mit>=CATALOG.threshold;
  const cur=reduced?Math.round(raw*0.85):raw;
  return {raw,mit,cur,reduced,pct:CATALOG.maxRaw?Math.round(cur/CATALOG.maxRaw*1000)/10:0};
}
const levelFor=p=>LEVELS.find(l=>p>=l.lo&&p<=l.hi)||LEVELS[0];
const answered=q=>{const v=valueOf(q.f);return !(v===""||v==null||(Array.isArray(v)&&!v.length));};
const scoredMissing=()=>pages().flatMap(p=>qsOf(p).filter(q=>q.p!=="none"&&q.o.length&&!answered(q)).map(q=>({q,p})));
const writtenMissing=()=>pages().flatMap(p=>qsOf(p).filter(q=>!q.o.length&&!answered(q)));
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function chrome(){
  const s=t();
  document.documentElement.lang=lang;
  $("langEn").setAttribute("aria-pressed",lang==="en");
  $("langFr").setAttribute("aria-pressed",lang==="fr");
  $("appName").textContent=s.app;
  $("dbDraft").innerHTML=`${s.draft} <b>${draftId}</b>`;
  $("dbCatalog").innerHTML=`${s.catalog} <b>v${CATALOG.version}</b>`;
  $("dbPhase").innerHTML=`${s.phase} <b>${phase()==="design"?"Design":"Implementation"}</b>`;
  $("btnSave").textContent=s.save; $("btnLoad").textContent=s.load;
  $("ftNote").textContent=s.footer;
}
function overview(){
  const s=t();
  $("ovTitle").textContent=s.ovTitle; $("ovLede").textContent=s.ovLede;
  $("ovCaption").textContent=s.ovCaption;
  ["thSection","thStatus","thAnswered","thRaw","thMit"].forEach(k=>$(k).textContent=s[k]);
  const body=$("trackerBody"); body.innerHTML="";
  let tR=0,tM=0,tD=0,tA=0;
  pages().forEach(p=>{
    const qs=qsOf(p), done=qs.filter(answered).length;
    let raw=0,mit=0;
    qs.forEach(q=>{const v=pointsFor(q); if(q.p==="raw")raw+=v; else if(q.p==="mitigation")mit+=v;});
    tR+=raw;tM+=mit;tD+=done;tA+=qs.length;
    const cls=done===qs.length&&qs.length?"done":done?"part":"none";
    const lab=cls==="done"?s.complete:cls==="part"?s.partial:s.notStarted;
    const tr=document.createElement("tr");
    tr.className="clickable"; tr.tabIndex=0;
    tr.onclick=()=>openPage(p.id);
    tr.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();openPage(p.id);}};
    tr.innerHTML=`<td><span class="sec-link">${esc(L(p))}</span></td>
      <td><span class="status ${cls}">${lab}</span></td>
      <td class="num tnum">${done} / ${qs.length}</td>
      <td class="num tnum">${p.type==="raw"?raw:`<span class="na">${s.na}</span>`}</td>
      <td class="num tnum">${p.type==="mitigation"?mit:`<span class="na">${s.na}</span>`}</td>`;
    body.appendChild(tr);
  });
  $("trackerFoot").innerHTML=`<tr><td>${s.total}</td><td></td>
    <td class="num tnum">${tD} / ${tA}</td><td class="num tnum">${tR}</td><td class="num tnum">${tM}</td></tr>`;
  readiness();
}
function readiness(){
  const s=t(), miss=scoredMissing(), soft=writtenMissing();
  $("rdTitle").textContent=s.rdTitle; $("rdLede").textContent=s.rdLede;
  $("rdNote").textContent=s.rdNote; $("btnExport").textContent=s.exportBtn;
  let html = miss.length===0
    ? `<div class="alert ok">${s.rdOk}</div>`
    : `<div class="alert bad">${s.rdMissing(miss.length)}</div>
       <ul class="missing-list">${miss.slice(0,60).map(({q,p})=>
        `<li><span>${esc(L(q))}</span><button class="go" onclick="jumpTo('${p.id}','${q.f}')">${esc(L(p))}</button></li>`).join("")}</ul>`;
  if(soft.length) html+=`<div class="alert warn">${s.rdSoft(soft.length)}</div>`;
  $("rdResult").innerHTML=html;
  $("btnExport").disabled=miss.length>0;
}
function section(){
  const s=t(), list=pages(), p=list.find(x=>x.id===currentPage)||list[0];
  currentPage=p.id;
  const idx=list.indexOf(p), qs=qsOf(p);
  $("navTitle").textContent=s.navTitle; $("btnBack").textContent=s.back;
  $("secStep").textContent=s.step(idx+1,list.length);
  $("secTitle").textContent=L(p);
  const nav=$("navList"); nav.innerHTML="";
  list.forEach(x=>{
    const q=qsOf(x), d=q.filter(answered).length;
    const cls=d===q.length&&q.length?"done":d?"part":"todo";
    const mark=cls==="done"?"\u2713":cls==="part"?"\u25D0":"\u25CB";
    const li=document.createElement("li");
    li.innerHTML=`<button ${x.id===p.id?'aria-current="true"':""} onclick="openPage('${x.id}')">
      <span class="tick ${cls}" aria-hidden="true">${mark}</span><span>${esc(L(x))}</span></button>`;
    nav.appendChild(li);
  });
  const wrap=$("qList"); wrap.innerHTML="";
  qs.forEach((q,i)=>{
    const div=document.createElement("div");
    div.className="q"; div.id="q-"+q.f;
    let tags="";
    if(q.req) tags+=`<span class="tag req">${s.required}</span>`;
    if(q.p==="raw") tags+=`<span class="tag pts">${s.pointsRaw}</span>`;
    if(q.p==="mitigation") tags+=`<span class="tag mit">${s.pointsMit}</span>`;
    if(q.vif) tags+=`<span class="tag cond">${s.conditional}</span>`;
    const v=valueOf(q.f);
    let ctl="", legend=`<legend>${i+1}. ${esc(L(q))}</legend>`;
    if(q.o.length&&q.t==="checkbox"){
      ctl=`<div class="opts">`+q.o.map((o,oi)=>{
        const sel=Array.isArray(v)&&v.includes(o.v);
        return `<label class="opt ${sel?"sel":""}"><input type="checkbox" ${sel?"checked":""}
          onchange="toggleBox('${q.f}',${oi})"><span>${esc(L(o))}</span>
          <span class="opt-pts">${o.p>0?"+"+o.p:"0"}</span></label>`;}).join("")+`</div>`;
    } else if(q.o.length&&q.t==="dropdown"){
      ctl=`<select onchange="setVal('${q.f}',this.value)"><option value="">${s.choose}</option>`+
        q.o.map(o=>`<option value="${esc(o.v)}" ${v===o.v?"selected":""}>${esc(L(o))}</option>`).join("")+`</select>`;
    } else if(q.o.length){
      ctl=`<div class="opts">`+q.o.map(o=>{
        const sel=v===o.v;
        return `<label class="opt ${sel?"sel":""}"><input type="radio" name="${q.f}" ${sel?"checked":""}
          onchange="setVal('${q.f}','${esc(o.v)}')"><span>${esc(L(o))}</span>
          <span class="opt-pts">${o.p>0?"+"+o.p:"0"}</span></label>`;}).join("")+`</div>`;
    } else if(q.t==="comment"){
      ctl=`<textarea placeholder="${s.placeholder}" oninput="setText('${q.f}',this.value)">${esc(v)}</textarea>`;
      legend=`<p class="q-text">${i+1}. ${esc(L(q))}</p>`;
    } else {
      ctl=`<input type="text" placeholder="${s.placeholder}" value="${esc(v)}" oninput="setText('${q.f}',this.value)">`;
      legend=`<p class="q-text">${i+1}. ${esc(L(q))}</p>`;
    }
    const g=lang==="en"?q.gen:(q.gfr||q.gen);
    const inner=`${legend}<div class="tags">${tags}</div>${ctl}`;
    div.innerHTML=(q.o.length?`<fieldset>${inner}</fieldset>`:inner)+
      (g?`<details class="guidance"><summary>${s.guidance}</summary><p>${esc(g)}</p></details>`:"");
    wrap.appendChild(div);
  });
  $("btnPrev").textContent=s.prev; $("btnPrev").style.visibility=idx===0?"hidden":"visible";
  $("btnNext").textContent=idx===list.length-1?s.finish:s.next;
  rail();
}
function rail(){
  const s=t(), sc=scores(), lvl=levelFor(sc.pct);
  $("scTitle").textContent=s.scTitle;
  $("scPct").textContent=sc.pct+"%";
  $("scLvl").textContent=`${lang==="en"?lvl.en:lvl.fr} — ${lang==="en"?lvl.dEn:lvl.dFr}`;
  $("scRawK").textContent=s.scRawK; $("scMitK").textContent=s.scMitK;
  $("scRawV").textContent=sc.raw; $("scMitV").textContent=sc.mit;
  $("ladder").innerHTML=LEVELS.map(l=>{
    const on=l===lvl, passed=sc.pct>l.hi;
    const fill=passed?100:on?((sc.pct-l.lo)/(l.hi-l.lo))*100:0;
    return `<div class="rung ${on?"on":""}"><div class="fill" style="width:${Math.max(0,Math.min(100,fill))}%"></div>
      <span class="lbl">${lang==="en"?l.en:l.fr}</span><span class="rng">${l.lo}\u2013${l.hi}%</span></div>`;}).join("");
  $("scBar").style.width=(CATALOG.maxMit?Math.min(100,sc.mit/CATALOG.maxMit*100):0)+"%";
  $("scMitNote").textContent=sc.reduced?s.mitMet
    :s.mitShort(Math.max(0,Math.ceil(CATALOG.threshold-sc.mit)),Math.round(CATALOG.threshold));
  const miss=scoredMissing().length;
  $("scRemain").className="remain"+(miss===0?" clear":"");
  $("scRemain").innerHTML=miss===0?`<b>${s.remain(0)}</b>`:`<b>${miss}</b> ${s.remain(miss)}`;
}
function renderAll(){chrome(); $("viewSection").classList.contains("on")?section():overview();}
function setVal(f,v){answers[f]=v; section();}
function toggleBox(f,oi){
  const q=CATALOG.pages.flatMap(p=>p.questions).find(x=>x.f===f);
  const cur=Array.isArray(answers[f])?answers[f].slice():[];
  const val=q.o[oi].v, i=cur.indexOf(val);
  i>=0?cur.splice(i,1):cur.push(val);
  answers[f]=cur; section();
}
function setText(f,v){
  answers[f]=v; rail();
  const list=pages();
  document.querySelectorAll("#navList button").forEach((b,i)=>{
    const q=qsOf(list[i]), d=q.filter(answered).length;
    const cls=d===q.length&&q.length?"done":d?"part":"todo";
    const el=b.querySelector(".tick");
    el.className="tick "+cls;
    el.textContent=cls==="done"?"\u2713":cls==="part"?"\u25D0":"\u25CB";
  });
}
function openPage(id){currentPage=id; $("viewOverview").classList.remove("on");
  $("viewSection").classList.add("on"); section(); window.scrollTo({top:0});}
function goOverview(){$("viewSection").classList.remove("on");
  $("viewOverview").classList.add("on"); overview(); window.scrollTo({top:0});}
function stepSection(d){
  const list=pages(), i=list.findIndex(x=>x.id===currentPage)+d;
  if(i<0) return;
  if(i>=list.length){goOverview(); return;}
  openPage(list[i].id);
}
function jumpTo(pid,f){
  openPage(pid);
  setTimeout(()=>{const el=$("q-"+f); if(!el)return;
    el.scrollIntoView({behavior:"smooth",block:"center"});
    el.classList.add("flagged"); setTimeout(()=>el.classList.remove("flagged"),2400);},60);
}
function setLang(l){lang=l; renderAll();}
function toast(m){const e=$("toast"); e.textContent=m; e.classList.add("on");
  clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove("on"),3000);}
function fileBody(){
  const sc=scores();
  return {aia_format:"1.0",draft_id:draftId,system_id:null,submission_id:null,
    catalog_version:CATALOG.version,answer_language:lang,saved_at:new Date().toISOString(),
    scores:{raw:sc.raw,mitigation:sc.mit,current:sc.cur,percent:sc.pct,level:levelFor(sc.pct).en},
    data:answers};
}
function download(o,name){
  const b=new Blob([JSON.stringify(o,null,2)],{type:"application/json"});
  const u=URL.createObjectURL(b), a=document.createElement("a");
  a.href=u; a.download=name; a.click(); URL.revokeObjectURL(u);
}
function saveJson(){download(fileBody(),`AIA-draft-${draftId}.json`); toast(t().tSaved);}
function exportFinal(){
  if(scoredMissing().length){toast(t().tBlocked); return;}
  download(fileBody(),`AIA-${draftId}-final.json`); toast(t().tExported);
}
function loadJson(ev){
  const f=ev.target.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=()=>{try{
      const d=JSON.parse(r.result);
      answers=d.data||d.answers||{};
      if(d.draft_id) draftId=d.draft_id;
      renderAll(); toast(t().tLoaded);
    }catch(e){toast(t().tBad);}};
  r.readAsText(f); ev.target.value="";
}
chrome(); overview();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
