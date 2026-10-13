"""Offline tests for P3S-v5 semantic response representation recovery."""

from src.p3sr import _gate_view, normalize_dimension_reviews


def test_dimension_review_object_is_converted_only_for_declared_dimensions():
    original = {
        "candidate_index": 0,
        "status": "accept",
        "reason": "supported",
        "claim_supported": True,
        "nature_supported": True,
        "dimension_reviews": {
            "application_maturity": "supported",
            "extra_dimension": "supported",
        },
        "revised_fields": None,
    }
    before, after, reason = normalize_dimension_reviews(
        original,
        ["application_maturity", "human_machine_authority"],
        "[PLACEHOLDER_VERBATIM_SOURCE_TEXT]",
    )
    assert before == original
    assert set(before) == set(after)
    assert after["revised_fields"] is None
    assert [item["dimension"] for item in after["dimension_reviews"]] == [
        "application_maturity",
        "human_machine_authority",
    ]
    assert after["dimension_reviews"][0]["supported"] is True
    assert after["dimension_reviews"][1]["supported"] is False
    assert after["dimension_reviews"][0]["supporting_span"] == "[PLACEHOLDER_VERBATIM_SOURCE_TEXT]"
    assert "extra_dimensions_ignored" in reason
    assert "missing_declared_dimensions_marked_unsupported" in reason


def test_negative_dimension_values_are_not_supported_and_noop_null_is_gate_only():
    original = {
        "candidate_index": 1,
        "status": "accept",
        "reason": "supported",
        "claim_supported": True,
        "nature_supported": True,
        "dimension_reviews": {
            "application_maturity": "unsupported",
            "human_machine_authority": "no direct support",
        },
        "revised_fields": None,
    }
    _, after, _ = normalize_dimension_reviews(
        original,
        ["application_maturity", "human_machine_authority"],
        "[PLACEHOLDER_VERBATIM_SOURCE_TEXT]",
    )
    assert [item["supported"] for item in after["dimension_reviews"]] == [False, False]
    assert after["revised_fields"] is None
    gate_view = _gate_view([after])[0]
    assert gate_view["revised_fields"] == {}
    assert after["revised_fields"] is None
