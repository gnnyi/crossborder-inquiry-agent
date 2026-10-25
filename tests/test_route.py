import json
from pathlib import Path

import pytest

from scoring.policy import intent_codes, risk_flags, risk_policy, route_levels
from scoring.route import UnknownFlag, derive_route, priority_floor

ROOT = Path(__file__).resolve().parents[1]


def test_no_flags_is_auto_reply():
    assert derive_route([]) == "AUTO_REPLY"


@pytest.mark.parametrize("flag", ["REFUND_REQUEST", "COMPENSATION", "PRICE_COMMIT", "DELIVERY_COMMIT", "REVIEW_THREAT"])
def test_draft_flags(flag):
    assert derive_route([flag]) == "DRAFT_FOR_REVIEW"


@pytest.mark.parametrize("flag", ["CHARGEBACK_LEGAL", "IDENTITY_MISMATCH", "INJECTION"])
def test_escalate_flags(flag):
    assert derive_route([flag]) == "ESCALATE"


def test_strictest_wins_regardless_of_order():
    assert derive_route(["REFUND_REQUEST", "CHARGEBACK_LEGAL"]) == "ESCALATE"
    assert derive_route(["CHARGEBACK_LEGAL", "REFUND_REQUEST"]) == "ESCALATE"
    assert derive_route(["REVIEW_THREAT", "PRICE_COMMIT"]) == "DRAFT_FOR_REVIEW"


def test_unknown_flag_raises():
    with pytest.raises(UnknownFlag):
        derive_route(["ANGRY"])


def test_priority_floor():
    assert priority_floor(["REVIEW_THREAT"]) == "P0"
    assert priority_floor(["CHARGEBACK_LEGAL", "REFUND_REQUEST"]) == "P0"
    assert priority_floor(["REFUND_REQUEST"]) is None


def test_policy_table_is_the_specified_one():
    pol = risk_policy()
    assert route_levels() == ["AUTO_REPLY", "DRAFT_FOR_REVIEW", "ESCALATE"]
    expected = {"REFUND_REQUEST": "DRAFT_FOR_REVIEW", "COMPENSATION": "DRAFT_FOR_REVIEW", "PRICE_COMMIT": "DRAFT_FOR_REVIEW",
                "DELIVERY_COMMIT": "DRAFT_FOR_REVIEW", "REVIEW_THREAT": "DRAFT_FOR_REVIEW", "CHARGEBACK_LEGAL": "ESCALATE",
                "IDENTITY_MISMATCH": "ESCALATE", "INJECTION": "ESCALATE"}
    assert {k: v["min_route"] for k, v in pol["flags"].items()} == expected


def test_contract_enums_match_policy_tables():
    for name in ["output.schema.json", "label.schema.json"]:
        schema = json.loads((ROOT / "contract" / name).read_text())
        assert schema["properties"]["risk_flags"]["items"]["enum"] == risk_flags()
        assert schema["properties"]["intents"]["items"]["enum"] == intent_codes()
