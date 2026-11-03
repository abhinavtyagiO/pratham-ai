from dataclasses import replace
from datetime import time

import pytest
from pydantic import ValidationError

from app.domain.conversations import (
    ModelContext,
    ResponseDraft,
    build_model_context,
    format_closing_time,
    render_response,
)
from app.domain.policies import PolicyEngine
from app.domain.types import (
    Action,
    BusinessContext,
    Intent,
    Outcome,
    OwnerRule,
    PolicyResult,
    classify_outcome,
)


@pytest.fixture
def rule():
    return OwnerRule(
        "r1",
        "b1",
        Intent.PRICE_ENQUIRY,
        Action.DO_NOT_DISCLOSE_PRICE,
        "Do not disclose prices; invite visit",
    )


@pytest.fixture
def business():
    return BusinessContext(
        "b1", "Tyre shop", "TYRE_RETAIL", "Demo", time(9), time(18, 30), "Asia/Kolkata", "hi-IN"
    )


def test_matching_owner_rule(rule):
    result = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, [rule])
    assert result.rule_id == "r1"
    assert result.decision == Action.DO_NOT_DISCLOSE_PRICE
    assert classify_outcome(result) == Outcome.RESOLVED


def test_highest_priority_wins_independent_of_input_order(rule):
    high = replace(rule, id="higher", priority=200, action=Action.REQUIRE_OWNER_ATTENTION)
    for rules in ([rule, high], [high, rule]):
        result = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, rules)
        assert result.rule_id == "higher"
        assert classify_outcome(result) == Outcome.NEEDS_ATTENTION


def test_equal_priority_has_stable_tie_break(rule):
    other = replace(rule, id="r0")
    result = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, [rule, other])
    assert result.rule_id == "r0"


@pytest.mark.parametrize("changes", [{"enabled": False}, {"business_id": "other"}])
def test_disabled_and_other_business_rules_do_not_apply(rule, changes):
    result = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, [replace(rule, **changes)])
    assert result.decision == Action.REQUIRE_OWNER_ATTENTION
    assert result.attention_reason == "POLICY_MISSING"


def test_unknown_intent_is_attention_even_if_rule_exists(rule):
    result = PolicyEngine().evaluate("b1", Intent.UNKNOWN, [replace(rule, intent=Intent.UNKNOWN)])
    assert result.rule_id is None
    assert classify_outcome(result) == Outcome.NEEDS_ATTENTION
    assert result.attention_reason == "UNKNOWN_INTENT"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (time(18, 30), "shaam 6:30"),
        (time(0), "raat 12:00"),
        (time(9, 5), "subah 9:05"),
        (time(12), "dopahar 12:00"),
        (time(21), "raat 9:00"),
    ],
)
def test_local_business_hours_format(value, expected):
    assert format_closing_time(value) == expected


def test_context_is_allowlisted_and_never_includes_prices_or_owner_free_text(business, rule):
    private = "SECRET_PRICE ₹12,345; available now"
    business = replace(business, name=private, address=private)
    rule = replace(rule, instruction=private)
    policy = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, [rule])
    context = build_model_context(business, policy)
    assert context.model_dump() == {
        "decision": Action.DO_NOT_DISCLOSE_PRICE,
        "closing_time_phrase": "shaam 6:30",
    }
    assert private not in context.model_dump_json()
    with pytest.raises(ValidationError):
        ModelContext(decision=Action.DO_NOT_DISCLOSE_PRICE, price=12345)


@pytest.mark.parametrize(
    "output",
    [
        {"phrasing": "₹3500"},
        {"text": "Tyre costs ₹3500"},
        {"phrasing": "POLITE", "price": 3500},
        {"closing_time": "11:30"},
    ],
)
def test_response_schema_has_no_free_text_or_price_capability(output):
    with pytest.raises(ValidationError):
        ResponseDraft.model_validate(output)


def test_both_approved_phrasings_invite_visit_and_cannot_assert_stock(business, rule):
    policy = PolicyEngine().evaluate("b1", Intent.PRICE_ENQUIRY, [rule])
    context = build_model_context(business, policy)
    for phrasing in ("POLITE", "BRIEF"):
        text = render_response(context, ResponseDraft(phrasing=phrasing))
        assert "shop par" in text and "6:30" in text
        assert "available" not in text and "₹" not in text
        assert "aaj" not in text and "khuli hai" not in text  # no live opening-hours promise


def test_unknown_context_excludes_business_facts(business):
    policy = PolicyResult(Intent.UNKNOWN, Action.REQUIRE_OWNER_ATTENTION, None, "UNKNOWN_INTENT")
    context = build_model_context(business, policy)
    assert context.closing_time_phrase is None
    text = render_response(context, ResponseDraft())
    assert "naam" in text and "query" in text
