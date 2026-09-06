import pytest
from pydantic import ValidationError

from agent_data_scope.policy import ScopePolicy, highest_tier, parse_scope_policy, tier_rank


def test_example_policy_loads_and_validates(policy: ScopePolicy) -> None:
    assert policy.version == 2
    assert policy.agent_group == "example"
    assert [tier.label for tier in policy.classification.tiers] == ["PNI", "BCI", "SCI"]
    assert policy.approval.status == "draft"
    assert policy.approval.approved_by is None
    assert policy.approval.change_log[0].on == "2026-09-06", "YAML dates stay strings, as in the TypeScript port"
    assert policy.defaults.unlisted_tools == "block"
    assert policy.defaults.on_block == "stop"
    assert policy.incidents.model_dump() == {"owner": "platform-team", "sensitive_tiers": ["SCI"], "necessity_floor": 0.25, "necessity_streak": 3, "feedback_key": "data_scope_incident_v1"}
    assert set(policy.agents) == {"example-investigator", "example-support-drafter"}
    assert policy.agents["example-investigator"].owner == "ops-team"
    assert policy.agents["example-support-drafter"].permit[0].fields == ["subject", "body"]


def test_tier_must_be_in_classification(policy: ScopePolicy) -> None:
    wrong = policy.model_dump()
    wrong["agents"]["example-support-drafter"]["permit"][0]["tier"] = "TOP_SECRET"
    with pytest.raises(ValidationError, match="tier TOP_SECRET is not in the classification"):
        ScopePolicy.model_validate(wrong)


def test_permit_needs_tier_when_classification_exists(policy: ScopePolicy) -> None:
    missing = policy.model_dump()
    missing["agents"]["example-support-drafter"]["permit"][0]["tier"] = None
    with pytest.raises(ValidationError, match=r"example-support-drafter\.get_incoming_message: missing tier"):
        ScopePolicy.model_validate(missing)


def test_sensitive_tiers_must_be_in_classification(policy: ScopePolicy) -> None:
    wrong = policy.model_dump()
    wrong["incidents"]["sensitive_tiers"] = ["ULTRA"]
    with pytest.raises(ValidationError, match="incidents.sensitive_tiers: ULTRA is not in the classification"):
        ScopePolicy.model_validate(wrong)


def test_unknown_keys_are_rejected(policy: ScopePolicy) -> None:
    extra = policy.model_dump()
    extra["surprise"] = True
    with pytest.raises(ValidationError):
        ScopePolicy.model_validate(extra)


def test_on_block_defaults_to_stop() -> None:
    text = """
version: 2
agent_group: g
purpose: p
defaults: {unlisted_tools: permit, writes: forbid}
forbid: {fields_everywhere: []}
agents:
  a: {}
"""
    loaded = parse_scope_policy(text)
    assert loaded.defaults.on_block == "stop"
    assert loaded.agents["a"].permit == []
    assert tier_rank(loaded, "anything") == -1
    assert highest_tier(loaded, [{"tier": "X"}]) is None


def test_tier_rank_and_highest(policy: ScopePolicy) -> None:
    assert tier_rank(policy, "SCI") == 2
    assert tier_rank(policy, None) == -1
    assert tier_rank(policy, "nope") == -1
    assert highest_tier(policy, [{"tier": "PNI"}, {"tier": "BCI"}, {}]) == "BCI"
    assert highest_tier(policy, ["PNI", None, "SCI"]) == "SCI"
    assert highest_tier(policy, []) is None
