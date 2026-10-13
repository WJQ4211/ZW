# P3b quality report

This run stops before P4/G1. It creates candidate Evidence only; no formal Evidence row, hypothesis, snapshot, or T1 input was used.

- source_count: 9
- extraction_batch_count: 15
- semantic_screen_batch_count: 15
- provider_call_count: 30
- raw_candidate_count: 65
- schema_valid_count: 52
- exact_quote_matched_count: 45
- deterministic_gate_passed_count: 44
- actual_same_model_semantic_screen_pass_count: 43
- final_candidate_count: 43
- final_candidate_acceptance_rate: 0.661538
- unsupported_dimension_removal_count: 2
- pending_g1_visual_review_count: 13
- pending_g1_translation_review_count: 43

The semantic screen is a real same-model separate call, not an independent model, human review, or expert review. No rate is called accuracy. Different-source equivalent claims retain a possible_corroboration_group for G1.
