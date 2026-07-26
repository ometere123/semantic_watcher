# SemanticWatcher

**A reusable GenLayer primitive that turns live web pages into a verified, on-chain stream of material change events.**

`SemanticWatcher` is infrastructure, not an application. It is the piece you inherit or subscribe to when your contract needs to react to off-chain state that has no API and no numeric feed.

---

## The problem

Plenty of on-chain logic wants to react to off-chain state that no oracle publishes: a refund clause changed, a whitelist was edited, a status page flipped to degraded, a DAO quietly reworded its charter, a vendor shortened its uptime commitment.

You cannot hash a web page to detect that. Ad slots, session identifiers, CSRF tokens, view counters, "last updated" stamps and A/B copy mutate on essentially every request, so a byte-diff fires constantly and tells you nothing. Here are two renderings of the same unchanged policy:

```
Customers may request a refund within 30 days of purchase.
Page views: 18422 | Last updated 2026-07-01T09:00:00Z
```
```
You can ask for your money back up to 30 days after buying.
Page views: 18987 | Last updated 2026-07-02T11:30:00Z
```

Every byte-oriented approach reports a change. Nothing that matters changed.

What is needed is a **judgement**: did anything that actually matters change? And that judgement has to be made under consensus, because a single off-chain watcher is just an oracle you have to trust.

## Why this needs GenLayer

### The trust problem, stated precisely

Two or more mutually distrusting parties depend on a single observation of a web page — and that page is very often controlled by **one of them**.

A vendor publishes a service agreement. A customer escrows a deposit against it. Both need an answer to *"did the agreement materially change?"* Neither can be the one who answers it. And the vendor, who controls the page, has an active incentive to make a change look cosmetic.

That is a trust problem, not an information problem. The information is public — anyone can open the URL. What is missing is an **answer nobody can unilaterally author.**

### The counterfactual test

Delete GenLayer from the design and see what survives.

| Approach | What breaks |
|---|---|
| **Off-chain watcher + signed feed** | The operator decides what "material" means and when a change happened. Every party must trust them. That is the exact trust assumption the escrow existed to remove. |
| **Chainlink or any price oracle** | There is no numeric quantity to report. "The refund window changed from 30 days to 7" is not a feed value. |
| **Content hash on-chain** | Ads, session ids, view counters and timestamps change the hash on every request. It fires constantly and proves nothing. |
| **Deterministic HTML parser** | Breaks the first time the vendor reorders a `<div>`. Worse, a vendor who *wants* to hide a change only has to reword it, and a parser cannot tell "30 days" from "one month". |
| **Optimistic oracle + human dispute** | Works, but costs days and a bond per observation. Unusable for a watch polled hourly. |
| **A single LLM call off-chain** | Someone still has to be trusted to have run it honestly and reported the output faithfully. |

The property that only GenLayer provides: **N independent validators each fetch the page themselves and each form their own judgement, and the transaction only lands if their judgements agree in meaning.** No node is privileged. No party authors the answer. Disagreement is visible rather than silently resolved by whoever was asked.

### Why it is not the patterns that get rejected

| Anti-pattern | Why this is not that |
|---|---|
| *"An AI app with GenLayer attached"* | The output is not advice, a recommendation or a summary for a human to read. It is a typed state transition — `version++`, a severity integer, a structured claim diff — consumed programmatically by other contracts. In `examples/sla_guard.py` it releases an escrow. |
| *"A validator that only checks output format"* | Neither equivalence principle looks at shape. Round 1 requires validators to agree that two claim sets carry the same **information**; round 2 requires the severity integers to **match exactly**. Valid JSON with a different verdict fails consensus. |
| *"Judging facts from user-submitted text"* | No fact about the world is ever accepted from a caller. The only thing a caller supplies is a URL, and it is **immutable after creation** — there is no `set_url`. Every recorded claim was fetched by the contract, from its own stored URL, inside a consensus block. |
| *"A thin LLM wrapper"* | The model is one step of five. Around it sit anchored canonicalization, a deterministic digest gate, a severity ladder, monotonic owner constraints, failure semantics that never mutate state, and a subscriber fan-out. Remove the consensus and the contract has no reason to exist; remove the surrounding machinery and the consensus is unusable. |

The output moves money. That is the honest test of whether consensus is decorative here: `SlaGuard` unlocks a deposit on `severity >= 3`. If the observation could be authored by one party, the escrow is worthless.

---

## Why each non-deterministic call is non-deterministic

Only two of the eight write methods enter a consensus block at all: `create_watch` and `poke`. Between them there are exactly **three** non-deterministic operations. Each one is listed here with the reason it cannot be anything else.

| Call | Where | Why it must be non-deterministic |
|---|---|---|
| `gl.nondet.web.render(url)` | round 1 | Network I/O. Two nodes fetching the same URL milliseconds apart legitimately receive different bytes. There is no deterministic way for a contract to learn the contents of a web page — the alternative is not "do it deterministically", it is "have someone tell you and trust them". |
| `gl.nondet.exec_prompt(extraction)` | round 1 | Reducing prose to canonical claims is a language-understanding task. A deterministic parser can extract a `<div>`; it cannot recognise that "you may return items within one month" and "30-day refund window" are the same claim, which is the entire point. Model inference is non-deterministic by construction. |
| `gl.nondet.exec_prompt(diff)` | round 2 | "Did the meaning change, and does it matter under this policy?" is irreducibly a judgement. There is no total function from two strings to a severity. This is the one question the whole contract exists to answer. |

### What is deliberately **not** non-deterministic

This matters as much as the list above. Every non-deterministic operation is a consensus risk and a cost, so the surface is kept as small as it can be. All of the following run as ordinary deterministic code:

- **The change decision itself.** `digest(claims) == stored_digest` is plain Keccak256 over a sorted claim list, computed outside every consensus block. When a page has not moved, "nothing changed" is a *deterministic* answer that no model participates in.
- **Access control.** Ownership checks, the monotonic `min_severity` and `cooldown` constraints, the paused check.
- **Cooldown arithmetic.** Timestamp parsing and comparison.
- **The severity gate.** `severity >= min_severity` is an integer comparison, not a judgement.
- **Storage, events, and subscriber fan-out.** Including which subscribers clear their own floor.
- **All input validation and output sanitisation.** URL scheme, policy length, claim de-duplication, severity clamping, JSON recovery.

The shape to notice: **the model is asked what the page says, never what the contract should do.** Every state transition, every payout-adjacent decision and every access check is deterministic code acting on a consensus-agreed observation.

### Ordering discipline

Inside `poke`, all deterministic guards run *before* the first consensus block — watch exists, not paused, cooldown elapsed. A caller who fails a guard never spends a consensus round. The digest gate then sits *between* the two rounds, so an unchanged page costs one round instead of two.

### Does the deterministic logic weaken the case for consensus?

It is a fair question — if so much is decided in ordinary code, why is consensus needed at all? The answer is that **the deterministic logic operates entirely on consensus-agreed data and cannot exist without it.**

Trace what the digest gate actually consumes:

```
digest(claims) == stored_digest
        │                │
        │                └── the previously agreed snapshot   ← consensus output
        └── the claim set extracted this round                ← consensus output
```

Both inputs are consensus outputs. Delete the consensus rounds and the gate has nothing to hash — there is no claim set, no snapshot, no page. The same is true of every other deterministic step: the severity comparison needs a severity that only round 2 can produce, and the subscriber fan-out needs a change record that only exists because validators agreed one occurred.

So the deterministic code is not an *alternative* to consensus. It is a **constraint on what the consensus output is permitted to do.** Both halves are load-bearing:

| Remove | Result |
|---|---|
| The consensus rounds | Nothing to check. No snapshot, no severity, no observation at all. The contract is inert. |
| The deterministic logic | The model decides. It can bump a version, wake every subscriber, and mutate the stored snapshot on nothing but its own say-so. |

This is also what GenLayer's own guidance asks for — *"design explicit validation and equivalence rules for every LLM, web, image, or other non-deterministic result."* The deterministic gates **are** those rules. A contract with a large non-deterministic surface and no deterministic constraints is not more GenLayer-native; it is less safe. Keeping the non-deterministic surface **small and essential** is the discipline.

One distinction worth being precise about, since there is a real anti-pattern nearby. *"Validators that only check output format"* is a criticism of a weak **equivalence principle**. Nothing here touches the EPs: round 1 still requires validators to agree that two claim sets carry the same information, and round 2 still requires the severity integers to match exactly. The deterministic checks run *after* consensus, in contract code. They add a constraint; they remove nothing from the equivalence principle.

---

## How it works

Each `poke()` runs a two-round pipeline.

```
                  ┌──────────────────────────────────────────┐
   poke(id) ─────▶│ ROUND 1 (nondet: web + LLM)              │
                  │   gl.nondet.web.render(url)              │
                  │   → canonical claim set                  │
                  │   EP: semantic equivalence of claims     │
                  └──────────────────┬───────────────────────┘
                                     │
                  ┌──────────────────▼───────────────────────┐
                  │ DETERMINISTIC GATE                       │
                  │   digest(claims) == stored digest?       │
                  │   yes → done. no second round, no spend. │
                  └──────────────────┬───────────────────────┘
                                     │ changed
                  ┌──────────────────▼───────────────────────┐
                  │ ROUND 2 (nondet: LLM)                    │
                  │   classify diff against policy           │
                  │   → severity 1..4 + change list          │
                  │   EP: identical severity verdict         │
                  └──────────────────┬───────────────────────┘
                                     │ severity >= min_severity
                  ┌──────────────────▼───────────────────────┐
                  │ version++, history, subscriber callbacks │
                  └──────────────────────────────────────────┘
```

### Round 1 — canonical semantic snapshot

The page is fetched inside the consensus block and reduced to an ordered set of `key → value` claims, restricted to what the watch's **policy** cares about. Volatile content is explicitly excluded.

```json
{"claims": [
  {"key": "cancellation_fee",   "value": "5%"},
  {"key": "eligibility",        "value": "account in good standing"},
  {"key": "refund_window_days", "value": "30"}
]}
```

### The hard part: anchored canonicalization

Making independent validators produce the *same* snapshot from the same page is the load-bearing problem in this design. Left alone, each node invents its own naming for the same claim — `refund_window` vs `refund_period` vs `return_window` — and the diff becomes pure noise. Nothing downstream can survive that.

The fix: **the previously agreed claim set is fed back into the extraction prompt as an anchor**, both keys and values. The model must reuse an existing key whenever the claim still exists, and must reproduce the previous value *verbatim* when the substance is unchanged.

Both halves are load-bearing, and measurement on live consensus is what proved it. An earlier build anchored keys only. Polling an unchanged page produced:

| key | poll 1 | poll 2 |
|---|---|---|
| `domain_purpose` | "for use in documentation examples" | "documentation examples" |
| `permission_required` | "no permission needed" | "none" |
| `usage_restriction` | "avoid use in operations" | "avoid use in operations" |

Keys were perfectly stable — key anchoring worked. But values drifted in phrasing, so the digest changed on every poll, the deterministic gate never fired, and a classification round ran every time. The version never bumped (round 2 correctly absorbed the drift), so the failure was invisible to any test that only checked for false events.

With value anchoring added, three consecutive polls of the same page produce a byte-identical digest and the gate fires as designed. `tests/integration/` now asserts digest stability, not just version stability.

This is also why the baseline snapshot is taken during `create_watch` rather than lazily. Without a baseline there are no anchors, and the first poll would report the entire page as new.

### The deterministic gate

A canonical digest of the claim set is computed **outside** any non-deterministic block:

```python
normalised = sorted((c["key"], c["value"]) for c in claims)
digest = Keccak256(json.dumps(normalised, separators=(",", ":")).encode()).hexdigest()
```

If it matches the stored digest, nothing changed: the second round is skipped entirely. Unchanged pages cost one round rather than two, and the "no change" answer is perfectly deterministic — no model involved.

### Round 2 — materiality, not difference

Only when the claim set actually moved. The diff is classified against the watch's natural-language policy:

| Severity | Meaning |
|---|---|
| 1 `COSMETIC` | Rewording, reordering, reformatting. A reader acting on the old snapshot would not be misled. |
| 2 `MINOR` | A real change that does not affect any decision the policy concerns. |
| 3 `MATERIAL` | The meaning of a policy-relevant claim changed. |
| 4 `CRITICAL` | A policy-relevant claim was reversed, removed, or invalidated. |

Only changes at or above the watch's `min_severity` bump the version, append to history, and notify subscribers.

Note what happens on a **cosmetic** change: no event fires, but **the snapshot still advances**. Otherwise every later diff is measured against increasingly stale text and the drift compounds until everything looks material.

### Equivalence principles

Both rounds use `gl.eq_principle.prompt_comparative`, and neither could use `strict_eq`. Two validators rendering the same page seconds apart legitimately see different bytes; agreement has to be about meaning.

**Round 1** — validators must agree on the *extracted information*. Differences in key naming, ordering, whitespace, casing and equivalent units are ignored. A different number, date, name, or a reversed statement is **not** equivalent. One node erroring while another succeeds is **not** equivalent.

**Round 2** — validators must agree on the *verdict*. Severity values must match exactly; the wording of the summary is irrelevant.

---

## Safety properties

These are the design rules the contract holds to, each backed by a test.

**A failed fetch is never interpreted as "the content was removed."**
The single most important property here. A downstream contract must never be told a clause vanished because of a 503. Failures increment a counter and emit `WatchDegraded` after three consecutive misses; they never touch the snapshot.

**Non-deterministic blocks return envelopes, not exceptions.**
Every round returns `{"ok": bool, ...}` so validators can agree *about a failure* rather than the transaction simply dying. Errors carry deterministic class prefixes — `EXPECTED`, `EXTERNAL`, `TRANSIENT`, `LLM_ERROR` — so callers can branch without parsing prose.

**An unclassifiable change is retained, not lost.**
If the claim set moved but the classifier failed, the old snapshot is kept. Advancing it would silently swallow a real change: the next poll would see no difference and the event would never fire.

**Model output is never trusted structurally.**
Fenced JSON is recovered, duplicate keys collapse, empty keys drop, values are whitespace-collapsed and length-capped, and severities outside `0..4` are clamped. An unparseable severity is treated as `MATERIAL` — over-reporting is the safe direction.

**Storage never enters a consensus block.**
Storage values are copied into plain Python locals before any non-deterministic closure. Each equivalence-principle block lives in a dedicated single-purpose method, so no storage write, message emission or nested block can end up inside one by accident.

**Everything unbounded is capped.**
64 claims, 32 history records (ring buffer), 32 subscribers, 24000 page characters. On-chain storage is not free and unbounded growth turns a cheap `poke()` into an unpayable one.

**The watch owner cannot suppress what subscribers signed up for.**
This one deserves its own section — see below.

---

## The suppression problem

The owner of a watch may well be the operator of the watched page. A vendor publishes a service agreement, lets counterparties subscribe against it, and then quietly mutes reports about their own changes. Every owner power has to be examined against that threat.

The rule: **owner controls may only ever make a watch more responsive, never less.**

| Power | Constraint |
|---|---|
| `url` | **No setter.** Repointing a watch would invalidate every subscriber's assumption about what is being observed. |
| `policy` | **No setter.** Same reasoning — the policy defines what "material" means. |
| `set_min_severity` | **May only be lowered.** Raising it would retroactively suppress changes subscribers subscribed in order to hear. Owners wanting a narrower feed create a second watch. |
| `set_cooldown` | **May only be lowered.** A long enough cooldown is indistinguishable from pausing. |
| `set_active` | Pausing stays available, but **cannot be silent**: it emits `WatchActiveChanged` and flips `reliable` to false. |
| `transfer_watch` | A new owner inherits the same monotonic constraints — no reset. |

On top of that, **the severity floor belongs to the subscriber, not the watch.** `subscribe(watch_id, min_severity)` records the threshold you chose, and nothing the owner does can raise it.

The one residual power is pausing. It is deliberately not removed — blocking it while subscribers exist would let a griefer lock an owner in permanently. Instead it is made loud, which is why consumers must gate on `reliable`:

```python
state = watcher.view().get_watch(watch_id)
if not state["reliable"]:
    ...   # paused or degraded: we do not know, so do not assume stability
```

**Silence from an unreliable watch means "we do not know", never "nothing changed."**

---

## Why this is reusable

"Reusable" is easy to claim, so here is the falsifiable version: **a consumer contract integrates in one method and needs to understand nothing about consensus.**

### The whole integration surface

```python
@gl.public.write
def on_watch_change(self, watch_id, version, severity, summary, diff_json) -> None:
    if gl.message.sender_address != self.watcher: raise ...
    if watch_id != self.watch_id: raise ...
    if int(severity) >= 3:
        self.unlocked = True
```

That is it. [`examples/sla_guard.py`](examples/sla_guard.py) is a complete worked consumer — a deposit escrow that releases when a vendor materially rewrites their agreement — and it contains **no web fetching, no prompts, no equivalence principles, no snapshot handling, no JSON parsing, no severity logic.** It reads one integer.

What a consumer never has to learn: how to write an equivalence principle, why `strict_eq` fails on live pages, how to keep validators converging on a canonical form, what to do when a fetch fails, or how to avoid mistaking downtime for deletion. Those are the parts that are hard to get right, and they are exactly the parts that live here instead of being reimplemented per project.

### What makes it a primitive rather than an application

| Property | Why it matters for reuse |
|---|---|
| **Zero domain assumptions** | The policy is a natural-language *parameter*, not code. The same deployed contract serves SLA monitoring, licence tracking, ToS insurance and governance-document watching with no change and no redeploy. Nothing about refunds, uptime or licensing appears anywhere in the source. |
| **One deployment, many watches, many subscribers** | Shared infrastructure. Consumers do not deploy their own copy; they call `create_watch` or `subscribe` on an existing one. Costs and the source-reputation of a watch amortise across everyone using it. |
| **An event source** | Deliberately the most composable output shape available. A push callback plus a pull-readable `version` means both reactive and polling consumers work without the contract knowing anything about them. |
| **Safe-by-default trust model** | A consumer does not have to audit the watch owner. The owner's powers are constrained *by the contract* — `min_severity` and `cooldown` may only be lowered, `url` and `policy` have no setter, and the subscriber picks its own severity floor. Reuse is only real if integrating does not require trusting whoever set the watch up. |
| **Honest failure surface** | One `reliable` flag covers both pausing and degradation. A consumer has exactly one thing to check before treating silence as stability. |
| **Typed interface** | `IWatchSubscriber` and `ISemanticWatcher` are importable stubs; integration is autocompleted and type-checked rather than stringly-typed. |

### Who would actually use it

Each of these is an existing on-chain need with no current answer, and each needs only the callback above:

- **SLA and uptime escrows** — a status page flips to degraded
- **Terms-of-service insurance** — a refund or liability clause is rewritten
- **DAO treasury guards** — a partner's governance document changes
- **Licence compliance** — upstream licensing terms shift under a dependency
- **Delisting and depeg detection** — an announcement lands before any price moves
- **Regulatory monitoring** — a published rule or threshold is amended

None of these are variations on one demo. They differ only in the policy string.

### The honest limits

Reuse claims should come with the cases where reuse is a bad idea:

- **Not for high-frequency data.** Two consensus rounds per change is the wrong tool for anything that moves per-block. Use a price feed.
- **Not for pages behind auth or heavy JavaScript.** `render` handles a lot, but a login wall stops it.
- **Round 1 does not always converge on the first attempt.** Observation rounds occasionally return `UNDETERMINED`; the transaction writes nothing and the call must be retried. Treat `create_watch` and `poke` as retryable.
- **Pausing remains an owner power.** It cannot be removed without letting a griefing subscriber lock an owner in permanently. It is made loud instead — hence `reliable`.

---

## Using it

### As a subscriber

Implement one method and call `subscribe`. See [`examples/sla_guard.py`](examples/sla_guard.py) for a complete worked example — a deposit escrow that unlocks when a vendor materially rewrites their service agreement.

```python
@gl.public.write
def on_watch_change(
    self,
    watch_id: u256,
    version: u32,
    severity: u8,
    summary: str,
    diff_json: str,
) -> None:
    # Both checks are mandatory. Without them anyone can forge this callback.
    if gl.message.sender_address != self.watcher:
        raise gl.vm.UserError("EXPECTED: caller is not the watcher")
    if watch_id != self.watch_id:
        raise gl.vm.UserError("EXPECTED: unexpected watch id")

    if int(severity) >= 3:
        self.withdrawal_unlocked = True
```

Callbacks are emitted `on='finalized'`, so a subscriber is never woken by a change that later gets reorganised away.

What the example contract does **not** contain is the point: no web fetching, no prompts, no equivalence principles, no snapshot handling. That is what makes `SemanticWatcher` a primitive rather than an application.

### As a direct reader

```python
watcher = ISemanticWatcher(watcher_address)
state = watcher.view().get_watch(watch_id)

if not state["reliable"]:
    ...            # paused or degraded; do not treat silence as stability
elif state["version"] > self.last_seen_version:
    ...            # something material happened
```

Always check `reliable` before treating an absence of events as evidence that nothing changed.

---

## API

### Lifecycle

| Method | |
|---|---|
| `create_watch(url, policy, min_severity=3, cooldown_seconds=3600, render_mode="text", wait_after_loaded="") -> u256` | Register a URL and take its baseline snapshot. Costs one observation round. |
| `poke(watch_id)` | Re-observe and record any material change. **Permissionless** — anyone may pay to advance a watch. The cooldown, not an access check, bounds the cost. |

### Subscriptions

| Method | |
|---|---|
| `subscribe(watch_id, min_severity=3)` | Register the caller for callbacks at a floor **they** choose. One entry per address. |
| `unsubscribe(watch_id)` | Remove the caller. |

### Owner controls

`set_active` · `set_min_severity` (lower only) · `set_cooldown` (lower only) · `transfer_watch`

There is deliberately no `set_url` and no `update_policy`.

### Views

`get_watch` · `get_claims` · `get_history` · `get_latest_change` · `get_subscribers` · `is_due` · `watch_count`

### Events

`WatchCreated` · `WatchPolled` · `MaterialChange` · `WatchDegraded` · `WatchActiveChanged` · `WatchSensitivityChanged`

---

## Development

```bash
pip install genvm-linter genlayer-test
```

Lint (must pass before anything else):

```bash
genvm-lint check contracts/semantic_watcher.py --json
```

Direct-mode tests — in-memory, web and model layers mocked, no node required:

```bash
pytest tests/direct/ -v
```

Integration tests — real consensus over live web and model calls:

```bash
gltest tests/integration/ -v -s --network studionet
```

### Test coverage

37 direct tests. The adversarial cases are the point of the suite; anyone can test a happy path.

| Area | Cases |
|---|---|
| Baseline | snapshot stored, claims sorted and deduplicated, input validation |
| Deterministic gate | identical claims skip the diff round; cosmetic change advances the snapshot without bumping the version |
| Material change | version bump, history record, `min_severity` filtering, severity clamping |
| Site misbehaving | fetch failure preserves the snapshot, empty page is a failure not a deletion, repeated failures degrade, a success clears the counter |
| Model misbehaving | fenced JSON recovered, unparseable extraction fails loudly, unclassifiable change retained not lost |
| Access control | owner-only mutators, ownership transfer, paused watches, unknown watch ids |
| **Suppression resistance** | min_severity cannot be raised, cooldown cannot be raised, a new owner inherits no reset, url/policy have no setters, pausing and degradation both surface through `reliable` |
| Subscriptions | idempotent subscribe, subscriber-chosen severity floor, floor range validation, targeted unsubscribe |
| Cooldown | rapid polling blocked |

### Notes on the environment

Two host-level workarounds live in `tests/conftest.py`. Neither affects contract behaviour:

- **Windows only.** `gltest`'s direct-mode loader unlinks a temp file still bound to fd 0. POSIX allows this; Windows raises `PermissionError`. The shim tolerates the failed unlink and sweeps the leaked files at exit. On Linux and macOS the block is skipped entirely.
- **Contract registry reset.** The SDK permits one `gl.Contract` subclass per process. Without clearing that between tests, a suite covering more than one contract passes or fails purely on file ordering.

One documentation discrepancy worth flagging: the storage guide shows constructing a storage dataclass with `DynArray[T]()` in memory. That raises `TypeError: this class can't be instantiated by user` on SDK v0.2.16. Storage-backed collections have to be allocated in a slot — this contract uses `TreeMap.get_or_insert_default()` and then populates fields.

## Layout

```
contracts/semantic_watcher.py   the primitive
examples/sla_guard.py           worked consumer example
tests/direct/                   in-memory tests, mocked web and model
tests/integration/              consensus tests against a real node
tests/conftest.py               host workarounds only
```

## Status

Lint clean. **37 direct tests pass. 4 integration tests pass against real StudioNet consensus**, including a full-surface run that exercises all 8 writes and reads all 7 views.

### Deployed

| | |
|---|---|
| Network | StudioNet (chain id 61999) |
| Address | `0x4307441035EDdd5Fe64aAec8321729321c8c498a` |
| Studio | https://studio.genlayer.com/?import-contract=0x4307441035EDdd5Fe64aAec8321729321c8c498a |
| Explorer | https://explorer-studio.genlayer.com/address/0x4307441035EDdd5Fe64aAec8321729321c8c498a |

All 8 write methods have been executed against this deployment, so the explorer shows the complete surface rather than a deploy and one call: `create_watch` ×2, `subscribe`, `unsubscribe`, `set_min_severity`, `set_cooldown`, `set_active` ×2, `poke`, `transfer_watch`. Watch #1 is live; watch #2 was created solely to demonstrate `transfer_watch` and now belongs to `0x1111…1111`.

### Measured on live consensus

A watch on `https://example.com/` with the policy *"The stated purpose of this domain and any usage or permission notes."* produced this canonical snapshot, agreed by validators:

```json
[
  {"key": "allowed_usage",          "value": "documentation_examples"},
  {"key": "permission_requirement", "value": "none"},
  {"key": "primary_purpose",        "value": "illustrative_examples"},
  {"key": "usage_restriction",      "value": "avoid_operational_use"}
]
```

Digest `71e196e221894a2188c28732e949ead0495ded776abacddbd669b9a47beab2d8`, **identical across three consecutive polls** — the deterministic gate fires and the classification round is skipped, exactly as designed.

### Observed consensus behaviour

Stated plainly, because anyone building on this should know it before they hit it:

- Individual validator votes routinely include `DISAGREE` and `IDLE`. Transactions still reach `ACCEPTED` on quorum. This is the equivalence principle doing its job on a genuinely non-deterministic observation.
- **An observation round can return `UNDETERMINED`**, meaning the validator set did not reach agreement. Nothing is written — no watch is created, no snapshot advances, no counter moves — and the call simply has to be retried. Observed once across the runs recorded here; the retry succeeded with `AGREE, AGREE, IDLE, IDLE, AGREE`.
- Deterministic writes (`subscribe`, `set_active`, `transfer_watch`, …) do not have this behaviour. Only `create_watch` and `poke` enter a consensus block.

Treat the two non-deterministic writes as **retryable**, not as guaranteed-first-attempt. A failed consensus round is safe — it is indistinguishable from never having called.

### Full public surface, exercised on chain

`tests/integration/test_full_surface.py` drives all 8 write methods and reads all 7 views against live consensus in one run, printing state after each step. It also asserts the negative cases: raising `min_severity` refused, raising `cooldown` refused, double-subscribe refused, poking a paused watch refused, the previous owner locked out after transfer, and the new owner still unable to raise `min_severity`.

```
gltest tests/integration/test_full_surface.py -v -s --network studionet
```

This is the fastest way for a reviewer to confirm that nothing in the contract is decorative.

### Roadmap

- Per-claim severity policies, so one watch can treat pricing as critical and contact details as minor
- Optional staked poking, letting a watch fund its own cadence
- A `render_mode="screenshot"` path for pages whose meaning is visual
