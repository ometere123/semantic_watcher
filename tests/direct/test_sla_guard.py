"""Direct-mode tests for the SlaGuard example.

The example exists to show that consuming SemanticWatcher is small. These
tests cover the part that is small but not trivial: a callback that unlocks
money must not be callable by anyone who feels like it.
"""

from conftest import as_address

GUARD = "examples/sla_guard.py"

# Direct mode permits a single contract class per process, so the watcher is
# not deployed alongside the guard here. direct_alice stands in for the
# watcher's address; the cross-contract path is covered by the integration
# tests instead.
WATCH_ID = 1


def deploy_guard(direct_deploy, watcher_addr, vendor_addr):
    return direct_deploy(
        GUARD, as_address(watcher_addr), WATCH_ID, as_address(vendor_addr)
    )


def test_material_change_unlocks_withdrawal(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    guard = deploy_guard(direct_deploy, direct_alice, direct_bob)
    assert guard.is_withdrawal_unlocked() is False

    # direct_alice stands in for the watcher address in this deployment.
    with direct_vm.prank(direct_alice):
        guard.on_watch_change(1, 2, 3, "Uptime commitment cut to 95%", "[]")

    assert guard.is_withdrawal_unlocked() is True
    breaches = guard.get_breaches()
    assert len(breaches) == 1
    assert breaches[0]["severity"] == 3


def test_cosmetic_change_is_recorded_but_does_not_unlock(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    guard = deploy_guard(direct_deploy, direct_alice, direct_bob)

    with direct_vm.prank(direct_alice):
        guard.on_watch_change(1, 2, 1, "Reworded the preamble", "[]")

    assert guard.is_withdrawal_unlocked() is False
    assert len(guard.get_breaches()) == 1


def test_callback_rejects_an_untrusted_caller(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    """Anyone able to forge this callback could drain the escrow."""
    guard = deploy_guard(direct_deploy, direct_alice, direct_bob)

    with direct_vm.prank(direct_charlie):
        with direct_vm.expect_revert("EXPECTED"):
            guard.on_watch_change(1, 2, 4, "forged", "[]")

    assert guard.is_withdrawal_unlocked() is False


def test_callback_rejects_a_foreign_watch_id(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """A trusted watcher hosts many watches; only ours may unlock this escrow."""
    guard = deploy_guard(direct_deploy, direct_alice, direct_bob)

    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert("EXPECTED"):
            guard.on_watch_change(99, 2, 4, "different watch", "[]")

    assert guard.is_withdrawal_unlocked() is False
