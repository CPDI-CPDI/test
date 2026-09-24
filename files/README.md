# Published AIA tables

Schema version 1.0. These files are written to a fixed contract: the
column names and their order do not change without the schema version changing
with them. Anything reading them can rely on that.

| Table | Rows | Columns |
|---|---|---|
| `answers` | 3932 | 15 |
| `catalog_versions` | 10 | 13 |
| `crosswalk_gaps` | 0 | 9 |
| `departments` | 102 | 4 |
| `og_records` | 38 | 16 |
| `og_resources` | 161 | 11 |
| `option_point_items` | 409 | 5 |
| `option_points` | 86 | 3 |
| `option_set_items` | 333 | 4 |
| `option_sets` | 61 | 3 |
| `question_map` | 1419 | 30 |
| `question_options` | 996 | 7 |
| `questions` | 206 | 14 |
| `section_versions` | 150 | 12 |
| `sections` | 27 | 11 |
| `services` | 0 | 12 |
| `submissions` | 39 | 22 |
| `system_services` | 0 | 11 |
| `systems` | 37 | 21 |

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

Join on `og_record_id`.

`system_id` is assigned by row order when the tables are built, so it moves
whenever a record is added or withdrawn. It is kept because it is easier to read
and because `submissions` and `answers` use it internally, but anything held
outside these files — a crosswalk, a saved report, a note — should key on the
record identifier, which never moves.

| From | To | On |
|---|---|---|
| `systems` | `submissions` | `system_id` |
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
