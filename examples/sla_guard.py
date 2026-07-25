# v0.2.18
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *

import json
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# SlaGuard -- a worked example of consuming SemanticWatcher.
#
# A vendor and a customer escrow a deposit against a published service
# agreement. If the vendor materially rewrites that agreement -- shortens the
# uptime commitment, removes the credit clause, changes the support window --
# the customer gets a withdrawal window that would otherwise be closed.
#
# The point of the example is what this contract does *not* contain: no web
# fetching, no prompts, no equivalence principles, no snapshot handling. It
# implements one callback and reads one severity integer. All of the
# observation machinery lives in SemanticWatcher, which is what makes that
# contract a primitive rather than an application.
#
# Deploy order:
#   1. deploy SemanticWatcher
#   2. watch_id = watcher.create_watch(agreement_url, policy, min_severity=3)
#   3. deploy SlaGuard(watcher_address, watch_id, vendor)
#   4. watcher.subscribe(watch_id)  -- called *by* the SlaGuard address
# ---------------------------------------------------------------------------


SEV_MATERIAL = 3

ERR_EXPECTED = "EXPECTED"


@allow_storage
@dataclass
class Breach:
    version: u32
    severity: u8
    summary: str
    observed_at: str


@gl.contract_interface
class ISemanticWatcher:
    class View:
        def get_watch(self, watch_id: u256) -> dict: ...

    class Write:
        def subscribe(self, watch_id: u256) -> None: ...


class WithdrawalUnlocked(gl.Event):
    def __init__(self, version: u32, severity: u8, /, **blob): ...


class SlaGuard(gl.Contract):
    watcher: Address
    watch_id: u256
    vendor: Address
    customer: Address
    withdrawal_unlocked: bool
    breaches: DynArray[Breach]

    def __init__(self, watcher: Address, watch_id: u256, vendor: Address):
        self.watcher = watcher
        self.watch_id = watch_id
        self.vendor = vendor
        self.customer = gl.message.sender_address
        self.withdrawal_unlocked = False

    # -- the entire integration surface -------------------------------------

    @gl.public.write
    def on_watch_change(
        self,
        watch_id: u256,
        version: u32,
        severity: u8,
        summary: str,
        diff_json: str,
    ) -> None:
        """Callback invoked by SemanticWatcher on a material change.

        Two checks matter here and they are the only security-relevant lines in
        this contract: the caller must be the watcher we trust, and the change
        must concern the watch we subscribed to. Without both, anyone could
        unlock the deposit by calling this method directly.
        """

        if gl.message.sender_address != self.watcher:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: caller is not the watcher")
        if watch_id != self.watch_id:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: unexpected watch id")

        record = self.breaches.append_new_get()
        record.version = version
        record.severity = severity
        record.summary = summary
        record.observed_at = str(len(self.breaches))

        if int(severity) >= SEV_MATERIAL:
            self.withdrawal_unlocked = True
            WithdrawalUnlocked(version, severity, summary=str(summary)).emit()

    # -- views --------------------------------------------------------------

    @gl.public.view
    def is_withdrawal_unlocked(self) -> bool:
        return bool(self.withdrawal_unlocked)

    @gl.public.view
    def get_breaches(self) -> list:
        return [
            {
                "version": int(b.version),
                "severity": int(b.severity),
                "summary": str(b.summary),
            }
            for b in self.breaches
        ]

    @gl.public.view
    def get_watch_state(self) -> dict:
        """Read the upstream watch directly, e.g. to surface a degraded feed."""
        return ISemanticWatcher(self.watcher).view().get_watch(self.watch_id)
