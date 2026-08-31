# AIA Master Table — build pipeline

Builds the AIA master table from source: the Open Government AIA collection and
the published questionnaire. Nothing is transcribed by hand and nothing is
carried over from the earlier workbook.

## Why this is a rebuild rather than a repair

The previous workbook had one row per system. That grain is wrong. A portal
record is a **system**, and a single record can hold several **submissions** —
the ATIP record alone holds at least four (an original, two 2021 updates and a
2022 update). Collapsing those into one row discarded most of the history.

It was also built by parsing PDFs, which is why it carried extraction confidence
scores and 500-plus question variants. Most records publish the tool's own JSON,
which needs no interpretation at all.

## Requirements

```
python 3.9+
pip install openpyxl          # step 5
pip install pypdf             # step 6 only, or install poppler-utils for pdftotext
```

Outbound access to `open.canada.ca`, `api.github.com` and
`raw.githubusercontent.com`.

## Running it

The scripts can sit in a `scripts/` subfolder or flat in your working folder;
outputs are written beside them either way. `00_selftest.py` prints the exact
output location, so check that line before running the rest.

```bash
python 00_selftest.py            # no network — confirms the logic works here
python 01_enumerate_portal.py    # portal records, resources, downloads JSON
python 02_parse_catalogs.py      # questionnaire -> sections, questions, options
python 03_bridge_questions.py    # stable IDs and parent chains across versions
python 04_ingest_submissions.py  # score every submission
python 06_pdf_only_triage.py     # which version each PDF-only record used
python 07_extract_pdf_answers.py # extract their answers, validated
python 05_build_workbook.py      # assemble the workbook
python 08_build_prototype.py     # optional: generate the questionnaire
```

**The order is 00, 01, 02, 03, 04, 06, 07, 05, 08 — not straight through.**
Step 5 comes after 07 because it merges the validated PDF assessments into the
same tables as the JSON ones; run it earlier and the workbook is built without
them. Step 8 is independent of 05 and can be run any time after 03.

Step 5 is run last on purpose: it merges the validated PDF assessments into the
same tables as the JSON ones. Run it before step 7 and the workbook is built
without them.

Each step writes CSVs into `data/`, so any step can be rerun on its own.
Downloads land in `raw/` and are never modified.

## What each step does

**00 — self-test.** Runs the parsing, bridging and scoring logic against
fixtures shaped like the real files. Confirms the mitigation rule reproduces six
published records exactly. Run this first.

**01 — enumerate the portal.** Reads the `aia` collection through the CKAN API,
records every file attached to every record, works out how many submissions each
record holds, and downloads the JSON. Prints the records holding more than one
submission and the records with no JSON. **This is the step that tells you how
big the backlog really is.**

**02 — parse the catalogs.** Fetches `src/survey-enfr.json` at every released
tag and parses each into sections, questions and answer options, computing the
maximum attainable raw and mitigation score per version.

Two things it fixes:

- The live tool encodes points inside the answer value: `item1-4` is worth 4.
  Change a question's weighting and every saved file silently becomes wrong.
  Here the score becomes its own column.
- Whether a question scores is carried in a panel-name suffix (`-RS`, `-NS`).
  The script reports every suffix it finds so an unrecognised one is visible
  rather than silently unscored.

A third thing it handles: mitigation questions come in paired **Design** and
**Implementation** pages. An assessment answers one set or the other according
to its project phase, so the attainable maximum is the larger branch, never the
sum. Summing them doubles the maximum, which halves every mitigation percentage
and stops the 15% reduction from ever firing.

The step checks itself against maxima derived independently from the published
scores — v0.10.0 max mitigation 46, v0.9.1 max mitigation 45 — and says plainly
whether it agrees. A disagreement means the point-type or phase mapping in
`common.py` needs adjusting, and everything downstream inherits the error.

**03 — bridge across versions.** Also derives the parent chain: a branching
condition names the question it depends on, so `parent_field_name`,
`root_field_name` and `chain_depth` fall out of data already parsed rather than
being authored by hand. A follow-up to a follow-up sits at depth 2 and resolves
to its root.

Sections are matched on page name, not on their displayed title. The three
de-risking areas all carry the title "De-Risking and Mitigation Measures", so
matching on the title collapsed six distinct pages into one section.

The parent is also folded into question matching. "Is this information publicly
available?" is asked under several different parents; wording alone merged eight
distinct questions into one.

**03 — details.** Assigns a stable numeric `question_uid` and
`section_uid` so a v0.9.1 assessment can sit beside a v1.0.1 one in the same
report. Field names cannot do this — they change, and are occasionally reused.

Matching runs strongest-evidence-first: identical field name, then near-identical
wording within a section, then wording anywhere. Every match records its method
and score, and anything below the threshold goes to `bridge_review.csv` rather
than being quietly accepted.

**04 — ingest submissions.** Scores every downloaded JSON against the catalog.
Scores are recomputed from the answers rather than copied from a summary, so a
disagreement with the published figure is visible. Systems and submissions are
written as separate tables, with `systems.current_submission_id` marking the
latest.

Free text keeps its language: English from `data`, French from
`translationsOnResult` where the department supplied it, with
`translation_source` recording which. Nothing is machine-translated.

**05 — build the workbook.** Assembles everything into `aia_master_table.xlsx`.
Values only, no formulas, no table objects — the convention Power BI expects.

It also folds the validated PDF assessments into `systems`, `submissions` and
`answers` rather than leaving them in separate sheets, so a report never has to
union two tables to count the portfolio. `source_format` distinguishes them.
Only assessments whose recomputed scores reproduced the scores printed in their
own document are merged; anything that failed validation stays in
`pdf_extract_review` and out of the data.

**Run this step last.**

**06 — PDF-only triage.** For records with no JSON, works out which
questionnaire version each PDF was answered under.

Publication date cannot answer this. Tested against the published records, every
version's date range overlaps the next: v0.9.1 assessments were published from
2021 to 2024, v0.10.0 from 2022 onward, and the most recent record on the portal
uses v0.10.0 while records published six months earlier use v1.0.1. Departments
complete assessments long before publication, and some reuse older saved files.

So the script resolves the version structurally: the PDF usually states its own
version, and where it does not, the question wording is matched against each
catalog and the best-explaining version wins.

Ranking uses the absolute number of matched questions rather than the matched
proportion. Proportion favours small catalogs — an early version with only a few
dozen distinctive questions can match most of them while explaining far less of
the document than a later version would. Publication date is reported alongside
as corroboration only.

**07 — extract PDF answers.** Reads the assessments that exist only as PDF.

The results PDF is generated by the tool and is highly regular, which makes
this far safer than free-text parsing usually is. It prints its own version, it
prints the raw, mitigation and current scores, every scored answer carries its
own `[ Points: +N ]`, and Section 3.2 and 3.3 separate raw impact from
mitigation explicitly. Points therefore come from the document itself rather
than from a lookup.

Nothing is accepted on trust. The extracted points are summed and compared with
the printed totals. A submission whose recomputed scores match its printed
scores had every scored answer found; one that does not is written to
`pdf_extract_review.csv` and **not** loaded, because a plausible-looking wrong
answer set is worse than a missing one.

Only rows with `validated = Y` should be merged into the master table.

**08 — build the prototype.** Generates a self-contained questionnaire from the
catalog. No question, option, point value or piece of guidance is written into
the page by hand: change the catalog, rerun, and the questionnaire changes. That
is the design document's claim about the questions table being the contract
between policy and technology, demonstrated rather than asserted.

Three things the real catalog forces that sample data did not:

- **Mitigation pages come in Design and Implementation pairs.** A system answers
  one branch according to its project phase, so the page list is filtered by the
  phase answer. Showing both would put the maximum at 154 instead of 77 and stop
  the 15% reduction from ever firing.
- **Branching is real.** `visible_if` decides whether a follow-up appears. A
  condition that cannot be parsed leaves the question visible: wrongly hiding a
  question is worse than wrongly showing one.
- **Only one of 295 questions carries the mandatory flag**, so readiness cannot
  rely on it. Completeness is measured against every visible scored question,
  with unanswered written follow-ups reported separately as a soft warning.

## Tables produced

| Layer | Table | Grain |
|---|---|---|
| Source | `og_records` | one portal record |
| | `og_resources` | one attached file |
| Assessment | `systems` | one system |
| | `submissions` | one completed AIA |
| | `answers` | one question in one submission |
| Catalog | `catalog_versions` | one AIA version, with its maxima |
| | `option_sets` / `option_set_items` | distinct answer label sets, stored once |
| | `option_point_profiles` | distinct scoring patterns |
| | `departments` | organisations, bilingual reference data |
| | `sections` / `section_versions` | section, canonical and per version |
| | `questions` / `question_map` | question, canonical and per version |
| | `question_options` | one answer option, points in a column |
| PDF | `pdf_submissions` / `pdf_answers` | PDF-only assessments, with validation |
| QA | `bridge_review`, `ingest_issues`, `pdf_triage`, `pdf_extract_review` | anything needing a person |

## Reading the results

- **Raw scores are not comparable across versions.** Use `current_score_pct`.
- **A count of submissions is not a count of systems in operation.** An
  assessment records a system at one moment in its life.
- **Check `bridge_review.csv` before any cross-version comparison.** Low-confidence
  matches are recorded, not hidden.
- **`point_type` of `unknown`** means step 2 met a panel suffix it did not
  recognise; those answers scored nothing.

## Known limits

- Historical catalogs depend on the repository's tags. If a version was never
  tagged, step 2 will not find it and step 4 will fall back to the nearest
  catalog it holds, noting this in `ingest_issues.csv`.
- Question bridging is textual. It handles rewording well and genuine
  replacement poorly, which is why weak matches are written out for review.
- Scanned PDFs have no extractable text and need OCR. Step 6 flags them.
- Some catalog versions were never tagged in the repository. `v1.0.1` is only
  reachable through `master`, and there is no `v0.10.1`. Step 4 records in
  `ingest_issues.csv` whenever a submission was scored against a near neighbour
  rather than its exact version.
