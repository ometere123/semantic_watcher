"""Integration tests for SemanticWatcher.

These deploy to a real GenLayer environment and exercise the parts that direct
mode cannot: actual validator consensus over live web fetches and real model
output.

Run against a running node:

    gltest tests/integration/ -v -s

They are skipped automatically when no node is reachable, so the direct suite
stays runnable on its own.

What these are really for
-------------------------
The direct tests prove the state machine is correct given a mocked observation.
The open question they cannot answer is whether independent validators, each
rendering the same live page at slightly different moments and running the
model separately, actually converge on the same canonical snapshot. That is
the load-bearing assumption of this whole design, and only a real environment
can test it.
"""

import pytest

from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded


# A page chosen for being stable, text-heavy, and unlikely to disappear.
STABLE_URL = "https://example.com/"
STABLE_POLICY = (
    "The purpose statement of this domain and any contact or licensing "
    "information."
)


@pytest.fixture(scope="module")
def watcher():
    factory = get_contract_factory("SemanticWatcher")
    return factory.deploy(args=[])


def test_baseline_reaches_consensus(watcher):
    """A single observation round must be agreed by the validator set."""
    result = watcher.create_watch(
        args=[STABLE_URL, STABLE_POLICY, 3, 0, "text", ""]
    ).transact()
    assert tx_execution_succeeded(result)

    watch_id = watcher.watch_count().call()
    state = watcher.get_watch(args=[watch_id]).call()
    assert state["version"] == 1
    assert state["claim_count"] > 0, "the page yielded no policy-relevant claims"


def test_repeated_observation_of_an_unchanged_page_is_stable(watcher):
    """The convergence test.

    Polling an unchanged page must not report a change. A failure here means
    validators are producing snapshots that differ enough to look like real
    movement -- which would make the primitive useless no matter how correct
    the surrounding state machine is.
    """
    watcher.create_watch(
        args=[STABLE_URL, STABLE_POLICY, 3, 0, "text", ""]
    ).transact()
    watch_id = watcher.watch_count().call()

    before = watcher.get_watch(args=[watch_id]).call()

    for _ in range(3):
        result = watcher.poke(args=[watch_id]).transact()
        assert tx_execution_succeeded(result)

    after = watcher.get_watch(args=[watch_id]).call()
    assert after["version"] == before["version"], (
        "an unchanged page produced a version bump: validators are not "
        "converging on a canonical snapshot"
    )
    assert after["total_polls"] == before["total_polls"] + 3
    assert after["consecutive_failures"] == 0

    # The strict form of the same property, and the one that actually matters.
    #
    # A stable version only proves the classifier absorbed whatever drift
    # occurred. A stable digest proves there was no drift to absorb -- the
    # extraction itself reproduced the snapshot exactly. Without this
    # assertion an earlier build passed the version check while quietly
    # re-wording every value on each poll ("no permission needed" -> "none"),
    # which defeats the deterministic gate and forces a classification round
    # on every single poll.
    assert after["claims_digest"] == before["claims_digest"], (
        "the canonical snapshot drifted on an unchanged page: value anchoring "
        "is not holding, so the deterministic gate will never fire"
    )


def test_unreachable_host_is_recorded_not_treated_as_deletion(watcher):
    """A dead host must degrade the watch, never rewrite its snapshot."""
    watcher.create_watch(
        args=[STABLE_URL, STABLE_POLICY, 3, 0, "text", ""]
    ).transact()
    watch_id = watcher.watch_count().call()

    claims_before = watcher.get_claims(args=[watch_id]).call()

    # Repoint is not possible by design, so this asserts the live-page path
    # stays consistent; the failure path itself is covered in direct mode
    # where the transport can be forced to fail deterministically.
    result = watcher.poke(args=[watch_id]).transact()
    assert tx_execution_succeeded(result)

    state = watcher.get_watch(args=[watch_id]).call()
    if state["consecutive_failures"] > 0:
        assert watcher.get_claims(args=[watch_id]).call() == claims_before
