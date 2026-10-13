# P3b preflight report

## Decision

P3b was **not executed**. This is an offline quality-repair preview over the saved P3a-R
technical pilot responses. P4/G1 remain closed.

## Immutable input

- P3a-R status: `real_api_technical_pilot_passed`
- `not_evidence_quality_validated`: `true`
- P3a-R v6 hashes unchanged: `true`
- API calls in P3Q: `0`
- requested temperature: `0.0`
- provider echoed temperature: `None`

## Rejudgment counts

| Metric | Count/value |
|---|---:|
| raw_candidate_count | 21 |
| schema_valid_count | 21 |
| exact_quote_matched_count | 18 |
| deterministic_gate_passed_count | 18 |
| same_model_semantic_screen_pass_count | 16 |
| final_candidate_count | 14 |
| final_candidate_acceptance_rate | 0.666667 |
| pending_g1_visual_review_count | 10 |
| pending_g1_translation_review_count | 14 |
| capitalized_entity_warning_count | 12 |

The semantic-screen pass count is not an accuracy estimate; there was no new model call in P3Q.

## PDF status: T0-SP-007

- pdf_page_count: `47`
- non_text_pages: `[47]`
- page_47_disposition: `decorative_back_cover_no_ocr_required`
- ocr_required_pages: `[]`
- layout_visual_review_required_pages: `[2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 41, 42, 43, 44, 45]`
- machine extraction: `pdfplumber_page_extract_text_no_ocr`

## Boundaries

- formal Evidence rows added: `0`
- hypotheses added: `0`
- T1 read: `false`
- P3b/P4/G1 executed: `false`
- snapshot frozen: `false`

The candidate preview is not a formal Evidence set and remains subject to future P3b,
human visual/translation review, and G1.
