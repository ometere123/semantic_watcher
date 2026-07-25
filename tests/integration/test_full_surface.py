"""End-to-end exercise of the entire public surface against live consensus.

Every write method is called and every view method is read, in lifecycle
order, with the state printed after each step. Run it with ``-s`` to see the
report:

    gltest tests/integration/test_full_surface.py -v -s --network studionet

This exists as much for reviewers as for regression: it is the shortest way to
see that nothing in the contract is decorative, and that the ordering
constraints described in the README are the ones actually enforced.
"""

import json

from gltest import get_contract_factory
from gltest.accounts import create_accounts
from gltest.assertions import tx_execution_failed, tx_execution_succeeded


URL = "https://example.com/"
POLICY = "The stated purpose of this domain and any usage or permission notes."

# The watch starts throttled so that lowering it later demonstrates the
# monotonic constraint *and* unblocks poke() in the same call.
INITIAL_COOLDOWN = 3600
INITIAL_MIN_SEVERITY = 3


def show(label, value):
    rendered = json.dumps(value, indent=2, sort_keys=True, default=str)
    print(f"\n  [READ] {label}\n{rendered}")


def step(n, label):
    print(f"\n{'=' * 70}\n  WRITE {n}: {label}\n{'=' * 70}")


def expect_refused(label, fn):
    """Call something that must be rejected, and report what happened.

    A reverting write does not raise here the way it does in direct mode --
    the transaction is mined and carries a failed execution result, so the
    receipt is what has to be inspected.
    """
    try:
        receipt = fn()
    except Exception as exc:
        first_line = str(exc).strip().splitlines()[0]
        print(f"  [REFUSED as designed] {label}\n      {first_line[:160]}")
        return True

    assert tx_execution_failed(receipt), (
        f"{label} was allowed but should have been refused"
    )
    print(f"  [REFUSED as designed] {label}")
    return True


def test_full_public_surface():
    other = create_accounts(1)[0]

    factory = get_contract_factory("SemanticWatcher")
    watcher = factory.deploy(args=[])
    print(f"\nDeployed SemanticWatcher at {watcher.address}")

    # -- WRITE 1 ------------------------------------------------------------
    step(1, "create_watch  (consensus round 1: fetch + canonicalise)")
    assert tx_execution_succeeded(
        watcher.create_watch(
            args=[URL, POLICY, INITIAL_MIN_SEVERITY, INITIAL_COOLDOWN]
        ).transact()
    )

    # -- all seven views, on a fresh watch ----------------------------------
    watch_id = watcher.watch_count().call()
    show("watch_count()", watch_id)

    baseline = watcher.get_watch(args=[watch_id]).call()
    show("get_watch(id)", baseline)
    show("get_claims(id)", watcher.get_claims(args=[watch_id]).call())
    show("get_history(id)  -- empty, no material change yet",
         watcher.get_history(args=[watch_id]).call())
    show("get_latest_change(id)  -- None, no material change yet",
         watcher.get_latest_change(args=[watch_id]).call())
    show("get_subscribers(id)  -- empty",
         watcher.get_subscribers(args=[watch_id]).call())
    show("is_due(id)  -- False, cooldown is still 3600s",
         watcher.is_due(args=[watch_id]).call())

    assert baseline["version"] == 1
    assert baseline["claim_count"] > 0
    assert baseline["reliable"] is True

    # -- WRITE 2 ------------------------------------------------------------
    step(2, "subscribe  (each subscriber picks its own severity floor)")
    assert tx_execution_succeeded(
        watcher.subscribe(args=[watch_id, 4]).transact()
    )
    assert tx_execution_succeeded(
        watcher.connect(other).subscribe(args=[watch_id, 2]).transact()
    )
    show("get_subscribers(id)  -- two subscribers, different floors",
         watcher.get_subscribers(args=[watch_id]).call())

    expect_refused(
        "subscribing twice from the same address",
        lambda: watcher.subscribe(args=[watch_id, 3]).transact(),
    )

    # -- WRITE 3 ------------------------------------------------------------
    step(3, "unsubscribe  (removes only the caller)")
    assert tx_execution_succeeded(
        watcher.connect(other).unsubscribe(args=[watch_id]).transact()
    )
    show("get_subscribers(id)  -- only the first subscriber remains",
         watcher.get_subscribers(args=[watch_id]).call())

    # The remaining subscriber is an EOA, not a contract. Drop it before
    # poking so a material change could not attempt an undeliverable callback.
    assert tx_execution_succeeded(watcher.unsubscribe(args=[watch_id]).transact())

    # -- WRITE 4 ------------------------------------------------------------
    step(4, "set_min_severity  (lower only -- the suppression guard)")
    expect_refused(
        "raising min_severity 3 -> 4",
        lambda: watcher.set_min_severity(args=[watch_id, 4]).transact(),
    )
    assert tx_execution_succeeded(
        watcher.set_min_severity(args=[watch_id, 1]).transact()
    )
    show("get_watch(id).min_severity  -- lowered 3 -> 1",
         watcher.get_watch(args=[watch_id]).call()["min_severity"])

    # -- WRITE 5 ------------------------------------------------------------
    step(5, "set_cooldown  (lower only -- a long cooldown is a silent pause)")
    expect_refused(
        "raising cooldown 3600 -> 86400",
        lambda: watcher.set_cooldown(args=[watch_id, 86400]).transact(),
    )
    assert tx_execution_succeeded(
        watcher.set_cooldown(args=[watch_id, 0]).transact()
    )
    show("get_watch(id).cooldown_seconds  -- lowered 3600 -> 0",
         watcher.get_watch(args=[watch_id]).call()["cooldown_seconds"])
    show("is_due(id)  -- True now the cooldown is gone",
         watcher.is_due(args=[watch_id]).call())

    # -- WRITE 6 ------------------------------------------------------------
    step(6, "set_active  (pausing cannot be silent)")
    assert tx_execution_succeeded(
        watcher.set_active(args=[watch_id, False]).transact()
    )
    paused = watcher.get_watch(args=[watch_id]).call()
    show("get_watch(id)  -- active False, reliable False",
         {"active": paused["active"], "reliable": paused["reliable"]})
    show("is_due(id)  -- False while paused",
         watcher.is_due(args=[watch_id]).call())
    assert paused["reliable"] is False

    expect_refused(
        "poking a paused watch",
        lambda: watcher.poke(args=[watch_id]).transact(),
    )

    assert tx_execution_succeeded(
        watcher.set_active(args=[watch_id, True]).transact()
    )
    show("get_watch(id).reliable  -- back to True after resume",
         watcher.get_watch(args=[watch_id]).call()["reliable"])

    # -- WRITE 7 ------------------------------------------------------------
    step(7, "poke  (round 1, then the deterministic gate)")
    assert tx_execution_succeeded(watcher.poke(args=[watch_id]).transact())

    after = watcher.get_watch(args=[watch_id]).call()
    show("get_watch(id)  -- after one poke", after)
    show("get_claims(id)  -- canonical snapshot",
         watcher.get_claims(args=[watch_id]).call())

    print("\n  --- convergence check -------------------------------------")
    print(f"  digest before poke : {baseline['claims_digest']}")
    print(f"  digest after  poke : {after['claims_digest']}")
    identical = after["claims_digest"] == baseline["claims_digest"]
    print(f"  identical          : {identical}")
    print(f"  version            : {baseline['version']} -> {after['version']}")
    print(f"  total_polls        : {baseline['total_polls']} -> {after['total_polls']}")

    assert after["total_polls"] == baseline["total_polls"] + 1
    assert after["claims_digest"] == baseline["claims_digest"], (
        "the snapshot drifted on an unchanged page -- value anchoring is not "
        "holding, so the deterministic gate never fires"
    )
    assert after["version"] == baseline["version"]

    # -- WRITE 8 ------------------------------------------------------------
    step(8, "transfer_watch  (new owner inherits the same constraints)")
    assert tx_execution_succeeded(
        watcher.transfer_watch(args=[watch_id, other.address]).transact()
    )
    show("get_watch(id).owner  -- transferred",
         watcher.get_watch(args=[watch_id]).call()["owner"])

    expect_refused(
        "the previous owner calling set_active after transfer",
        lambda: watcher.set_active(args=[watch_id, False]).transact(),
    )
    expect_refused(
        "the NEW owner raising min_severity -- no reset on transfer",
        lambda: watcher.connect(other).set_min_severity(
            args=[watch_id, 4]
        ).transact(),
    )

    print(f"\n{'=' * 70}")
    print("  8/8 writes exercised, 7/7 views read.")
    print(f"{'=' * 70}\n")
