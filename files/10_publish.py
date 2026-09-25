"""
STEP 10 — Publish the tables against a fixed contract.

Writes the tables into published/ in a form a reporting tool can read directly
from the repository and keep reading after the next rebuild.

The working files in data/ are convenient but not a contract. Their columns come
out in whatever order the code happens to produce, a file disappears when it has
nothing to report, and a column that is empty in one run and populated in the
next changes type underneath whatever is reading it. Any of those quietly breaks
a saved report.

So the published files hold to five rules:

  * Columns are fixed in name and order by contracts.py. Anything the contract
    does not name is dropped; anything it names that is missing is written empty.
  * Every file is always written, even with no rows, so a reference never
    points at nothing.
  * Text is UTF-8 with no byte order mark, which otherwise attaches itself to
    the first column name.
  * Line breaks inside answers are kept and properly quoted. The connection
    guidance sets the quote style, since a reader that does not will split one
    answer across several rows.
  * Diagnostics stay out. Confidence measures and extraction notes belong to
    the people checking the data, not to a report.

A manifest is written alongside, recording the schema version, every column and
the row count. A report can read it to notice that something moved.

Outputs
    published/*.csv
    published/_manifest.csv
    published/_schema.json
"""
from __future__ import annotations
import csv, json
from datetime import datetime, timezone
from pathlib import Path
from common import DATA_DIR, PUBLISH_DIR, log, header, assemble_core_tables
from contracts import CONTRACTS, SCHEMA_VERSION, PUBLIC, SOURCE

OUT = PUBLISH_DIR   # the repository root when run inside a clone


# systems, submissions and answers are assembled — JSON, validated PDFs and
# the crosswalk together — exactly as the workbook assembles them.
_CORE: dict | None = None


def core_tables() -> dict:
    global _CORE
    if _CORE is None:
        _CORE = assemble_core_tables()
    return _CORE


def read_source(name: str):
    if name in ("systems", "submissions", "answers"):
        return core_tables().get(name)
    path = DATA_DIR / SOURCE.get(name, f"{name}.csv")
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def tidy(value) -> str:
    """
    Normalise a value for publication.

    Line breaks inside written answers are kept, because the paragraphs are
    meaningful, but carriage returns are removed so that the files read the same
    on every platform. Everything is written as text; typing is left to whatever
    reads the file, since a column that is blank in one run and numeric in the
    next would otherwise change type without warning.
    """
    if value is None:
        return ""
    s = str(value)
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def main():
    header("STEP 10 — Publishing the tables")
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"  schema version {SCHEMA_VERSION}")
    log(f"  writing to {OUT}\n")

    manifest, drift, missing_tables = [], [], []

    for name in sorted(CONTRACTS):
        if name not in PUBLIC:
            continue
        cols = CONTRACTS[name]
        rows = read_source(name)

        if rows is None:
            missing_tables.append(name)
            rows = []

        # A column present in the data but not in the contract is a change
        # nobody has agreed to yet, so it is reported rather than published.
        if rows:
            extra = [c for c in rows[0] if c not in cols]
            absent = [c for c in cols if c not in rows[0]]
            if extra:
                drift.append((name, "not in the contract", extra))
            if absent:
                drift.append((name, "in the contract but not produced", absent))

        path = OUT / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore",
                               quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: tidy(r.get(c)) for c in cols})

        manifest.append({
            "table": name,
            "file": f"{name}.csv",
            "schema_version": SCHEMA_VERSION,
            "column_count": len(cols),
            "row_count": len(rows),
            "columns": "|".join(cols),
        })
        flag = "  (no source file)" if not rows and name in missing_tables else ""
        log(f"  {name:<24} {len(rows):>6} rows  {len(cols):>2} cols{flag}")

    # ---------------------------------------------------------------- manifest
    with open(OUT / "_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["table", "file", "schema_version",
                                          "column_count", "row_count", "columns"],
                           lineterminator="\n")
        w.writeheader(); w.writerows(manifest)

    schema = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "encoding": "utf-8",
        "byte_order_mark": False,
        "line_terminator": "\\n",
        "quoting": "minimal, with line breaks preserved inside quoted fields",
        "stable_key_note": ("og_record_id is the key. It is one to one with a "
                            "system and does not move. system_ref is a short "
                            "number for reading, assigned by row order, and "
                            "shifts when a record is added or withdrawn."),
        "tables": {m["table"]: {"columns": m["columns"].split("|"),
                                "row_count": m["row_count"]} for m in manifest},
    }
    (OUT / "_schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- guidance
    (OUT / "README.md").write_text(GUIDE.format(
        version=SCHEMA_VERSION,
        tables="\n".join(f"| `{m['table']}` | {m['row_count']} | {m['column_count']} |"
                          for m in manifest),
    ), encoding="utf-8")

    # ---------------------------------------------------------------- summary
    header("SUMMARY")
    log(f"  tables published : {len(manifest)}")
    log(f"  total rows       : {sum(m['row_count'] for m in manifest)}")

    if missing_tables:
        log(f"\n  written empty because no source file was found:")
        for t in missing_tables:
            log(f"    {t}")
        log("  The file still exists with its header, so a report reading it")
        log("  returns no rows rather than failing.")

    if drift:
        log("\n  COLUMNS THAT DO NOT MATCH THE CONTRACT")
        for table, kind, cols in drift:
            log(f"    {table}: {kind}")
            for c in cols:
                log(f"      {c}")
        log("\n  A column produced but not in the contract is not published.")
        log("  To publish it, add it to the end of the list in contracts.py.")
        log("  Adding at the end is safe; renaming, reordering or removing is")
        log("  not, and needs the schema version to move.")
    else:
        log("\n  every table matches its contract")

    log(f"\n  Commit the published folder. Point the report at the raw file URLs")
    log(f"  and read _manifest.csv first, so a change in the schema version or a")
    log(f"  row count is visible rather than silent.")


GUIDE = """# Published AIA tables

Schema version {version}. These files are written to a fixed contract: the
column names and their order do not change without the schema version changing
with them. Anything reading them can rely on that.

| Table | Rows | Columns |
|---|---|---|
{tables}

## Reading them from a report

Use the raw file address, not the page you see when browsing the repository:

```
https://raw.githubusercontent.com/OWNER/REPO/main/published/systems.csv
```

Pointing at `main` always gives the newest data. Pointing at a tag instead gives
a fixed copy that never moves, which is what you want for a report that has to
reproduce a published figure months later.

## The one setting that matters

Written answers contain line breaks. They are correctly quoted, but a reader
that does not expect them splits a single answer across several rows — in the
answers table that turns 3,932 rows into roughly 7,700, and no error is raised.

Set the quote style explicitly:

```
let
    Source = Csv.Document(
        Web.Contents("https://raw.githubusercontent.com/OWNER/REPO/main/published/answers.csv"),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true])
in
    Promoted
```

`QuoteStyle.Csv` is the important part. `Encoding = 65001` is UTF-8; the files
carry no byte order mark, so the first column name arrives clean.

Every value is written as text. Set the types you need in the report rather than
letting them be guessed, because a column that is empty in one rebuild and
populated in the next would otherwise change type on its own.

## Joining

`og_record_id` is the key throughout. It is the Open Government record
identifier, it is one to one with a system, and it does not move.

`system_ref` sits alongside it as a short number for reading — 1, 2, 3 rather
than a long identifier. It is assigned by row order, so it shifts whenever a
record is added or withdrawn. Never join on it, and never keep it outside these
files.

`submission_id` is the record identifier and a sequence number, so it is stable
too: adding a record does not renumber anything.

| From | To | On |
|---|---|---|
| `systems` | `submissions` | `og_record_id` |
| `submissions` | `answers` | `submission_id` |
| `submissions` | `catalog_versions` | `catalog_version` |
| `answers` | `question_map` | `field_name` **and** `catalog_version` |
| `question_map` | `questions` | `question_uid` |
| `systems` | `system_services` | `og_record_id` |

Joining `answers` to `question_map` on the field name alone mixes questions
together: a field name is only unique within one version of the form.

## Noticing a change

`_manifest.csv` lists every table with its schema version, column list and row
count. Reading it first means a change in the schema version, or a row count
that has moved further than expected, is something the report can show rather
than something that quietly alters a figure.

## What is not here

Confidence measures, extraction notes and review queues stay in the working
files. They are for checking the data, and in a report they invite doubt about
figures that are sound.

## Reporting on scores

Use `current_score_pct`. Raw point totals are not comparable between versions of
the form, because the highest attainable score differs. `catalog_versions` holds
the maximum for each version if you need to work it out yourself.
"""


if __name__ == "__main__":
    main()
