# Column contracts for the published tables.
#
# The order and spelling here is the contract. A report reading these files
# depends on both, so a change to this file is a change other people notice.
# Adding a column at the end is safe. Renaming, reordering or removing one is
# not, and requires the schema version to move.

SCHEMA_VERSION = "2.0"

CONTRACTS = {
    "systems": [
        "og_record_id", "system_ref", "system_name_en", "system_name_fr", "department_en",
        "department_fr", "department_code", "portal_department_code", "branch",
        "submission_count", "first_submission_label", "latest_submission_label",
        "current_submission_id", "current_impact_level", "current_score_pct", "portal_url_en",
        "service_count", "service_ids", "service_names_en", "service_match_confidence",
        "crosswalk_status"
    ],
    "submissions": [
        "submission_id", "og_record_id", "sequence_no", "system_ref", "submission_label",
        "catalog_version", "stated_version", "version_source", "publication_date",
        "raw_impact_score", "mitigation_score", "current_score", "reduction_applied",
        "current_score_pct", "mitigation_pct", "impact_level", "answer_count",
        "unmapped_answers", "source_format", "source_file", "extraction_method", "is_current"
    ],
    "answers": [
        "submission_id", "og_record_id", "catalog_version", "field_name", "question_uid",
        "point_type", "points", "answer_value_raw", "option_index", "answer_text_en",
        "answer_text_fr", "is_free_text", "translation_source", "mapped", "shown"
    ],
    "questions": [
        "question_uid", "canonical_text_en", "canonical_text_fr", "section_uid", "point_type",
        "answer_type", "first_seen_version", "last_seen_version", "version_count",
        "field_names", "reworded", "status", "parent_question_uid", "is_follow_up"
    ],
    "question_map": [
        "field_id", "catalog_version", "field_name", "page_name", "section_name_en",
        "section_name_fr", "section_order", "panel_name", "panel_suffix", "point_type",
        "mitigation_phase", "answer_type", "is_mandatory", "visible_if", "option_count",
        "max_points", "text_en", "text_fr", "guidance_en", "guidance_fr", "question_uid",
        "parent_field_name", "root_field_name", "chain_depth", "section_uid", "match_method",
        "match_score", "needs_review", "parent_question_uid", "root_question_uid"
    ],
    "question_options": [
        "catalog_version", "field_name", "field_id", "option_set_id", "point_profile_id",
        "option_count", "max_points"
    ],
    "option_sets": [
        "option_set_id", "option_count", "labels"
    ],
    "option_set_items": [
        "option_set_id", "option_index", "text_en", "text_fr"
    ],
    "option_points": [
        "point_profile_id", "option_set_id", "points_pattern"
    ],
    "option_point_items": [
        "point_profile_id", "option_set_id", "option_index", "option_value", "points"
    ],
    "departments": [
        "department_code", "name_en", "name_fr", "first_seen_version"
    ],
    "sections": [
        "section_uid", "display_name_en", "name_en", "name_fr", "page_name", "display_order",
        "point_type", "mitigation_phase", "first_seen_version", "last_seen_version",
        "bridged_with_fuzzy_match"
    ],
    "section_versions": [
        "section_uid", "catalog_version", "page_name", "section_name_en", "section_name_fr",
        "display_order", "point_type", "mitigation_phase", "max_raw_points",
        "max_mitigation_points", "match_method", "match_score"
    ],
    "catalog_versions": [
        "catalog_version", "git_ref", "question_count", "scored_question_count",
        "section_count", "max_raw", "max_mitigation", "max_mitigation_design",
        "max_mitigation_implementation", "max_mitigation_if_summed", "mitigation_threshold",
        "unclassified_questions", "source_url"
    ],
    "og_records": [
        "og_record_id", "title_en", "title_fr", "department_en", "department_fr",
        "department_code", "record_released", "record_modified", "date_published", "keywords",
        "homepage", "portal_url_en", "portal_url_fr", "resource_count",
        "submission_count_estimate", "has_json"
    ],
    "og_resources": [
        "resource_id", "og_record_id", "resource_name", "format", "language", "resource_type",
        "submission_label", "is_assessment_artifact", "download_url", "size_bytes",
        "last_modified"
    ],
    "services": [
        "service_id", "fiscal_year", "service_name_en", "owner_org_code", "owner_org_en",
        "owner_org_fr", "program_id", "program_name_en", "service_description_en",
        "declares_automation", "automation_description_en", "service_uri_en"
    ],
    "system_services": [
        "og_record_id", "system_ref", "service_id", "service_name_en", "service_fiscal_year",
        "match_confidence", "match_rationale", "source", "review_notes", "system_in_master",
        "system_id_at_crosswalk_time"
    ],
    "crosswalk_gaps": [
        "gap_type", "og_record_id", "system_name_en", "department_en", "service_id",
        "service_name_en", "match_confidence", "reason", "candidate"
    ],
}


# Tables anyone may read. The rest are working files: confidence measures,
# extraction diagnostics and review queues. They are useful for checking the
# data and misleading in a report, because they invite doubt about figures that
# are sound.
PUBLIC = {
    "systems", "submissions", "answers", "questions", "question_map",
    "question_options", "option_sets", "option_set_items", "option_points",
    "option_point_items", "departments", "sections", "section_versions",
    "catalog_versions", "og_records", "og_resources",
    "services", "system_services", "crosswalk_gaps",
}

# Where each published file is built from.
SOURCE = {
    "option_points": "option_point_profiles.csv",
    "option_point_items": "option_point_profile_items.csv",
}
