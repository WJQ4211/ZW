# P3Q audit correction

Input `P3Q-20260826T023500Z-offline-v2` remains read-only; its full hash set is recorded in the JSON correction.

`src/p3q.py::_dimension_support` is an `offline_keyword_dimension_heuristic`, not a model semantic review, not an independent model, and not a human review.

- inherited_v6_same_model_claim_nature_pass_count: 21
- offline_keyword_dimension_screen_pass_count: 16
- hybrid_preview_pass_count: 14
- deprecated old same_model_semantic_screen_pass_count: 16

Every corrected dimension review is explicitly marked `review_origin=offline_keyword_heuristic`, `model_generated=false`, and `human_reviewed=false`. Keyword spans are not described as semantic judgments.
