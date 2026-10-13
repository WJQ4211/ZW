# P3Q validator change log

Run: `P3Q-20260826T023500Z-offline-v2`  
Input: `P3AR-20260826T010914-real-api-v6`

- Historical P3a remains `deterministic_hardcoded_fixture` and is not eligible for G1.
- P3a-R is recorded here as `real_api_technical_pilot_passed`, with
  `not_evidence_quality_validated=true`; the immutable P3a-R directory was not edited.
- Capitalized entities are warnings (`needs_semantic_review`), not deterministic hard rejections.
- Hard gates remain: strict schema, in-scope source, real locator, normalized exact quote,
  non-empty Chinese translation, no unsupported numeric addition, no pseudo-continuous locator,
  and no plan/intent coded as an observed result.
- Semantic screening is named `same_model_separate_call_semantic_screening`; it is a quality
  screen only and cannot replace G1.
- Coding dimensions are reviewed one by one with `dimension`, `supported`,
  `supporting_span`, and `reason`. Unsupported dimensions are removed in the preview and
  recorded; all-unsupported candidates are held for human review.
- PDF OCR/extraction status is page-level. Machine text is preserved; any future G1 correction
  must use `human_corrected_excerpt` without overwriting `machine_extracted_excerpt`.
- Metrics are separated into raw, schema, quote, deterministic, semantic-screen and final
  counts. A same-model screen pass is not reported as extraction accuracy.
- Requested temperature is recorded as `0.0`; provider echo is `null` because no provider call
  was made by P3Q.

P3Q did not call an API, read T1, write formal Evidence, generate hypotheses, execute P3b,
execute P4/G1, or freeze a snapshot.
