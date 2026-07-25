"""Direct-mode tests for SemanticWatcher.

These run the contract in-memory with the web and model layers mocked, so the
whole state machine -- baseline, cosmetic drift, material change, failure
handling, access control -- is exercised without a node.

The adversarial cases are the point of this file. Anyone can test the happy
path; what determines whether this primitive is safe to build on is what it
does when the page is down, when the model returns garbage, and when the
change is real but the wording is not.
"""

import json

from conftest import as_address

CONTRACT = "contracts/semantic_watcher.py"

URL = "https://example.com/terms"
POLICY = "Refund window, cancellation fees, and eligibility requirements."

# Regexes matched against the prompt text of each round.
EXTRACT_PROMPT = r"You extract a canonical"
DIFF_PROMPT = r"You classify how significant"

PAGE_V1 = """
Refund Policy
Customers may request a refund within 30 days of purchase.
A cancellation fee of 5% applies.
Only accounts in good standing are eligible.
Page views: 18422 | Last updated 2026-07-01T09:00:00Z
"""

PAGE_V2_COSMETIC = """
Refund Policy
You can ask for your money back up to 30 days after buying.
We charge a 5% cancellation fee.
Eligibility requires an account in good standing.
Page views: 18987 | Last updated 2026-07-02T11:30:00Z
"""

PAGE_V3_MATERIAL = """
Refund Policy
Customers may request a refund within 7 days of purchase.
A cancellation fee of 25% applies.
Only accounts in good standing are eligible.
Page views: 19244 | Last updated 2026-07-03T08:15:00Z
"""


def claims(refund_days="30", fee="5%"):
    return json.dumps(
        {
            "claims": [
                {"key": "cancellation_fee", "value": fee},
                {"key": "eligibility", "value": "account in good standing"},
                {"key": "refund_window_days", "value": refund_days},
            ]
        }
    )


def verdict(severity, summary="changed", changes=None):
    return json.dumps(
        {
            "severity": severity,
            "summary": summary,
            "changes": changes
            or [{"key": "refund_window_days", "before": "30", "after": "7"}],
        }
    )


def mock_page(direct_vm, body):
    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 200, "body": body})


def baseline(direct_vm, direct_deploy, min_severity=3, cooldown=0):
    """Deploy and create a watch with a known baseline snapshot."""
    contract = direct_deploy(CONTRACT)
    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims())
    watch_id = contract.create_watch(
        URL, POLICY, min_severity=min_severity, cooldown_seconds=cooldown
    )
    direct_vm.clear_mocks()
    return contract, watch_id


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_baseline_snapshot_is_stored(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    state = contract.get_watch(watch_id)
    assert state["version"] == 1
    assert state["active"] is True
    assert state["total_polls"] == 1
    assert state["claim_count"] == 3

    stored = contract.get_claims(watch_id)
    assert [c["key"] for c in stored] == [
        "cancellation_fee",
        "eligibility",
        "refund_window_days",
    ]
    assert contract.get_history(watch_id) == []


def test_claims_are_sorted_and_deduplicated(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)
    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(
        EXTRACT_PROMPT,
        json.dumps(
            {
                "claims": [
                    {"key": "zebra", "value": "last"},
                    {"key": "alpha", "value": "  first   claim  "},
                    {"key": "alpha", "value": "duplicate ignored"},
                    {"key": "", "value": "empty key dropped"},
                ]
            }
        ),
    )
    watch_id = contract.create_watch(URL, POLICY, cooldown_seconds=0)

    stored = contract.get_claims(watch_id)
    assert [c["key"] for c in stored] == ["alpha", "zebra"]
    # Whitespace inside values is collapsed so formatting churn is not a diff.
    assert stored[0]["value"] == "first claim"


def test_create_watch_rejects_invalid_input(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)

    with direct_vm.expect_revert("EXPECTED"):
        contract.create_watch("ftp://example.com", POLICY)

    with direct_vm.expect_revert("EXPECTED"):
        contract.create_watch(URL, "")

    with direct_vm.expect_revert("EXPECTED"):
        contract.create_watch(URL, POLICY, min_severity=9)


# ---------------------------------------------------------------------------
# The deterministic gate
# ---------------------------------------------------------------------------


def test_identical_claims_skip_the_diff_round(direct_vm, direct_deploy):
    """An unchanged claim set must not reach the classifier at all.

    This is the cost-control property: pages that have not meaningfully moved
    cost one round, not two. If the gate regresses, the diff mock below would
    be needed and its absence would surface as a failure.
    """
    contract, watch_id = baseline(direct_vm, direct_deploy)

    # Different page text, identical extracted meaning.
    mock_page(direct_vm, PAGE_V2_COSMETIC)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims())
    # Deliberately no diff-round mock registered.
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["version"] == 1
    assert state["total_polls"] == 2
    assert contract.get_history(watch_id) == []


def test_cosmetic_change_advances_snapshot_without_bumping_version(
    direct_vm, direct_deploy
):
    """Cosmetic drift must be absorbed, not accumulated.

    The snapshot has to advance even though no event fires -- otherwise every
    later diff is measured against increasingly stale text.
    """
    contract, watch_id = baseline(direct_vm, direct_deploy)

    mock_page(direct_vm, PAGE_V2_COSMETIC)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="30 days"))
    direct_vm.mock_llm(DIFF_PROMPT, verdict(1, "wording only", []))
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["version"] == 1, "cosmetic change must not bump the version"
    assert contract.get_history(watch_id) == []

    stored = contract.get_claims(watch_id)
    value = next(c["value"] for c in stored if c["key"] == "refund_window_days")
    assert value == "30 days", "snapshot must advance even on a cosmetic change"


# ---------------------------------------------------------------------------
# Material change
# ---------------------------------------------------------------------------


def test_material_change_bumps_version_and_records_history(
    direct_vm, direct_deploy
):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    mock_page(direct_vm, PAGE_V3_MATERIAL)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="7", fee="25%"))
    direct_vm.mock_llm(
        DIFF_PROMPT, verdict(3, "Refund window cut from 30 to 7 days")
    )
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["version"] == 2

    history = contract.get_history(watch_id)
    assert len(history) == 1
    assert history[0]["severity"] == 3
    assert history[0]["version"] == 2
    assert "30 to 7" in history[0]["summary"]
    assert history[0]["changes"][0]["key"] == "refund_window_days"

    latest = contract.get_latest_change(watch_id)
    assert latest["version"] == 2


def test_min_severity_filters_changes_below_threshold(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy, min_severity=4)

    mock_page(direct_vm, PAGE_V3_MATERIAL)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="7"))
    direct_vm.mock_llm(DIFF_PROMPT, verdict(3, "material but below threshold"))
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["version"] == 1, "severity 3 is below a min_severity of 4"
    assert contract.get_history(watch_id) == []
    # The snapshot still advanced, so the change is not re-reported forever.
    assert state["claims_digest"] != ""


def test_severity_out_of_range_is_clamped(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    mock_page(direct_vm, PAGE_V3_MATERIAL)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="7"))
    direct_vm.mock_llm(DIFF_PROMPT, verdict(99, "model exceeded its own scale"))
    contract.poke(watch_id)

    assert contract.get_latest_change(watch_id)["severity"] == 4


# ---------------------------------------------------------------------------
# Adversarial: the site and the model misbehaving
# ---------------------------------------------------------------------------


def test_fetch_failure_does_not_erase_the_snapshot(direct_vm, direct_deploy):
    """A site being down is not the same as its content being removed.

    This is the single most important safety property here: a downstream
    contract must never be told a clause vanished because of a 503.
    """
    contract, watch_id = baseline(direct_vm, direct_deploy)
    before = contract.get_claims(watch_id)

    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 503, "body": ""})
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["version"] == 1
    assert state["consecutive_failures"] == 1
    assert contract.get_claims(watch_id) == before
    assert contract.get_history(watch_id) == []


def test_repeated_failures_mark_the_watch_degraded(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 503, "body": ""})
    for _ in range(3):
        contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["consecutive_failures"] == 3
    assert state["degraded"] is True


def test_a_successful_poll_clears_the_failure_counter(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 503, "body": ""})
    contract.poke(watch_id)
    assert contract.get_watch(watch_id)["consecutive_failures"] == 1

    direct_vm.clear_mocks()
    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims())
    contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["consecutive_failures"] == 0
    assert state["degraded"] is False


def test_empty_page_is_treated_as_a_failure_not_as_deletion(
    direct_vm, direct_deploy
):
    contract, watch_id = baseline(direct_vm, direct_deploy)
    before = contract.get_claims(watch_id)

    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 200, "body": "   "})
    contract.poke(watch_id)

    assert contract.get_claims(watch_id) == before
    assert contract.get_watch(watch_id)["consecutive_failures"] == 1


def test_fenced_json_from_the_model_is_recovered(direct_vm, direct_deploy):
    """Models wrap JSON in code fences. That must not cost a whole round."""
    contract = direct_deploy(CONTRACT)
    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(
        EXTRACT_PROMPT,
        "Here is the snapshot:\n```json\n" + claims() + "\n```\n",
    )
    watch_id = contract.create_watch(URL, POLICY, cooldown_seconds=0)

    assert contract.get_watch(watch_id)["claim_count"] == 3


def test_unparseable_extraction_fails_the_baseline_loudly(
    direct_vm, direct_deploy
):
    contract = direct_deploy(CONTRACT)
    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(EXTRACT_PROMPT, "I'm sorry, I can't help with that.")

    with direct_vm.expect_revert("LLM_ERROR"):
        contract.create_watch(URL, POLICY, cooldown_seconds=0)


def test_unclassifiable_change_is_retained_not_lost(direct_vm, direct_deploy):
    """If the claim set moved but the classifier failed, keep the old snapshot.

    Advancing it would silently swallow a real change: the next poll would see
    no difference and the event would never fire.
    """
    contract, watch_id = baseline(direct_vm, direct_deploy)
    before = contract.get_claims(watch_id)

    mock_page(direct_vm, PAGE_V3_MATERIAL)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="7"))
    direct_vm.mock_llm(DIFF_PROMPT, "not json at all")
    contract.poke(watch_id)

    assert contract.get_claims(watch_id) == before
    assert contract.get_watch(watch_id)["version"] == 1

    # The change is still detectable on the next poll.
    direct_vm.clear_mocks()
    mock_page(direct_vm, PAGE_V3_MATERIAL)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims(refund_days="7"))
    direct_vm.mock_llm(DIFF_PROMPT, verdict(3))
    contract.poke(watch_id)

    assert contract.get_watch(watch_id)["version"] == 2


# ---------------------------------------------------------------------------
# Access control and lifecycle
# ---------------------------------------------------------------------------


def test_only_the_owner_can_pause_a_watch(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("EXPECTED"):
            contract.set_active(watch_id, False)

    contract.set_active(watch_id, False)
    assert contract.get_watch(watch_id)["active"] is False


def test_a_paused_watch_cannot_be_poked(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)
    contract.set_active(watch_id, False)

    with direct_vm.expect_revert("EXPECTED"):
        contract.poke(watch_id)


def test_ownership_transfer_moves_control(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    contract.transfer_watch(watch_id, as_address(direct_bob))

    with direct_vm.expect_revert("EXPECTED"):
        contract.set_active(watch_id, False)

    with direct_vm.prank(direct_bob):
        contract.set_active(watch_id, False)
    assert contract.get_watch(watch_id)["active"] is False


def test_transfer_accepts_a_hex_string_address(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Calldata off the wire delivers addresses as hex strings, not Address.

    A direct-mode test that hands over a hand-built Address hides this: the
    contract passed here while failing on a real network with
    "AttributeError: 'str' object has no attribute 'as_bytes'".
    """
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    new_owner = str(as_address(direct_bob))
    contract.transfer_watch(watch_id, new_owner)

    assert contract.get_watch(watch_id)["owner"].lower() == new_owner.lower()


def test_transfer_to_the_zero_address_is_refused(
    direct_vm, direct_deploy, direct_alice
):
    """Transferring to zero would strand the watch with no owner forever."""
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    with direct_vm.expect_revert("EXPECTED"):
        contract.transfer_watch(watch_id, "0x" + "0" * 40)


def test_unknown_watch_is_rejected(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)
    with direct_vm.expect_revert("EXPECTED"):
        contract.get_watch(999)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def test_subscribe_is_idempotent_per_address(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    contract.subscribe(watch_id)
    assert len(contract.get_subscribers(watch_id)) == 1

    with direct_vm.expect_revert("EXPECTED"):
        contract.subscribe(watch_id)

    with direct_vm.prank(direct_bob):
        contract.subscribe(watch_id)
    assert len(contract.get_subscribers(watch_id)) == 2


def test_subscriber_chooses_its_own_severity_floor(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy, min_severity=1)

    contract.subscribe(watch_id, 4)
    with direct_vm.prank(direct_bob):
        contract.subscribe(watch_id, 2)

    floors = {s["subscriber"]: s["min_severity"] for s in contract.get_subscribers(watch_id)}
    assert sorted(floors.values()) == [2, 4]


def test_subscribe_rejects_a_floor_outside_the_scale(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    with direct_vm.expect_revert("EXPECTED"):
        contract.subscribe(watch_id, 0)

    with direct_vm.expect_revert("EXPECTED"):
        contract.subscribe(watch_id, 5)


def test_unsubscribe_removes_only_the_caller(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy)

    contract.subscribe(watch_id)
    with direct_vm.prank(direct_bob):
        contract.subscribe(watch_id)

    contract.unsubscribe(watch_id)
    remaining = contract.get_subscribers(watch_id)
    assert len(remaining) == 1

    with direct_vm.expect_revert("EXPECTED"):
        contract.unsubscribe(watch_id)


# ---------------------------------------------------------------------------
# Owner powers cannot be used to suppress
#
# The threat: the owner of a watch may be the operator of the watched page.
# Without these constraints a vendor could publish an agreement, let others
# subscribe against it, then quietly mute reports about their own changes.
# ---------------------------------------------------------------------------


def test_min_severity_cannot_be_raised(direct_vm, direct_deploy):
    """Raising the threshold would retroactively suppress subscribed events."""
    contract, watch_id = baseline(direct_vm, direct_deploy, min_severity=2)

    with direct_vm.expect_revert("only be lowered"):
        contract.set_min_severity(watch_id, 4)

    assert contract.get_watch(watch_id)["min_severity"] == 2


def test_min_severity_can_be_lowered(direct_vm, direct_deploy):
    """Making a watch more sensitive is always allowed."""
    contract, watch_id = baseline(direct_vm, direct_deploy, min_severity=3)

    contract.set_min_severity(watch_id, 1)
    assert contract.get_watch(watch_id)["min_severity"] == 1


def test_cooldown_cannot_be_raised(direct_vm, direct_deploy):
    """A long enough cooldown is indistinguishable from pausing the watch."""
    contract, watch_id = baseline(direct_vm, direct_deploy, cooldown=60)

    with direct_vm.expect_revert("only be lowered"):
        contract.set_cooldown(watch_id, 86400)

    assert contract.get_watch(watch_id)["cooldown_seconds"] == 60

    contract.set_cooldown(watch_id, 10)
    assert contract.get_watch(watch_id)["cooldown_seconds"] == 10


def test_a_new_owner_inherits_no_extra_power(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Transferring a watch must not reset the monotonic constraints."""
    direct_vm.sender = direct_alice
    contract, watch_id = baseline(direct_vm, direct_deploy, min_severity=2)

    contract.transfer_watch(watch_id, as_address(direct_bob))

    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("only be lowered"):
            contract.set_min_severity(watch_id, 4)


def test_url_and_policy_have_no_setters(direct_vm, direct_deploy):
    """Repointing a watch would invalidate every subscriber's assumption."""
    contract, _ = baseline(direct_vm, direct_deploy)

    assert not hasattr(contract, "set_url")
    assert not hasattr(contract, "update_policy")


def test_pausing_is_visible_through_the_reliable_flag(direct_vm, direct_deploy):
    """Silence from a paused watch must not read as 'nothing changed'."""
    contract, watch_id = baseline(direct_vm, direct_deploy)
    assert contract.get_watch(watch_id)["reliable"] is True

    contract.set_active(watch_id, False)
    state = contract.get_watch(watch_id)
    assert state["active"] is False
    assert state["reliable"] is False

    contract.set_active(watch_id, True)
    assert contract.get_watch(watch_id)["reliable"] is True


def test_a_degraded_watch_is_not_reliable(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy)

    direct_vm.mock_web(r".*example\.com/terms.*", {"status": 503, "body": ""})
    for _ in range(3):
        contract.poke(watch_id)

    state = contract.get_watch(watch_id)
    assert state["active"] is True
    assert state["degraded"] is True
    assert state["reliable"] is False


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_blocks_rapid_polling(direct_vm, direct_deploy):
    contract, watch_id = baseline(direct_vm, direct_deploy, cooldown=86400)

    mock_page(direct_vm, PAGE_V1)
    direct_vm.mock_llm(EXTRACT_PROMPT, claims())

    with direct_vm.expect_revert("cooldown"):
        contract.poke(watch_id)
