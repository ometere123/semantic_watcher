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

The contract fetches the page **itself**, inside a consensus block, and independent validators must agree on what it means. There is no privileged reporter and no signed feed to trust.

| | |
|---|---|
| **Not** a Chainlink feed | There is no numeric quantity to report. The question is semantic. |
| **Not** an off-chain watcher | Then the operator decides what "material" means, and you trust them. |
| **Not** a content hash | Hashes fire on ads and timestamps and stay silent on nothing at all. |
| **Not** a thin LLM wrapper | The model produces an observation; **consensus** is what makes it usable on-chain. |

This contract never accepts a claim about the world from user-submitted text. Every fact it records was fetched by the contract, from the URL registered in its own storage, inside a consensus block.

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

The fix: **the previously agreed claim keys are fed back into the extraction prompt**, and the model is required to reuse an existing key whenever the underlying claim still exists. Keys stay stable across polls instead of being re-invented on every run.

This is why the baseline snapshot is taken during `create_watch` rather than lazily. Without a baseline there are no anchors, and the first poll would report the entire page as new.

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

if state["degraded"]:
    ...            # the feed is stale; do not treat silence as stability
elif state["version"] > self.last_seen_version:
    ...            # something material happened
```

Always check `degraded` before treating an absence of events as evidence that nothing changed.

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
| `subscribe(watch_id)` | Register the caller for callbacks. One entry per address. |
| `unsubscribe(watch_id)` | Remove the caller. |

### Owner controls

`set_active` · `set_min_severity` · `set_cooldown` · `transfer_watch`

### Views

`get_watch` · `get_claims` · `get_history` · `get_latest_change` · `get_subscribers` · `is_due` · `watch_count`

### Events

`WatchCreated` · `WatchPolled` · `MaterialChange` · `WatchDegraded`

---

## What it is good for

- **SLA and uptime contracts** — a status page flips to degraded
- **Terms-of-service insurance** — a refund or liability clause is rewritten
- **DAO treasury guards** — a partner's governance document changes
- **License compliance** — upstream licensing terms shift
- **Delisting and depeg detection** — announcements that precede any price move
- **Regulatory monitoring** — a published rule or threshold is amended

The shape is deliberately the most composable one available: an event source.

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
gltest tests/integration/ -v -s
```

### Test coverage

26 direct tests. The adversarial cases are the point of the suite; anyone can test a happy path.

| Area | Cases |
|---|---|
| Baseline | snapshot stored, claims sorted and deduplicated, input validation |
| Deterministic gate | identical claims skip the diff round; cosmetic change advances the snapshot without bumping the version |
| Material change | version bump, history record, `min_severity` filtering, severity clamping |
| Site misbehaving | fetch failure preserves the snapshot, empty page is a failure not a deletion, repeated failures degrade, a success clears the counter |
| Model misbehaving | fenced JSON recovered, unparseable extraction fails loudly, unclassifiable change retained not lost |
| Access control | owner-only mutators, ownership transfer, paused watches, unknown watch ids |
| Subscriptions | idempotent subscribe, targeted unsubscribe |
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

## Status and roadmap

Lint passes, 26 direct tests pass. The integration suite is written but needs a funded testnet account or a local node to run — the convergence question it tests (do independent validators actually agree on a canonical snapshot of a live page?) is the one open risk in this design, and it can only be answered against real validators.

Planned next:

- Per-claim severity policies, so one watch can treat pricing as critical and contact details as minor
- Optional staked poking, letting a watch fund its own cadence
- A `render_mode="screenshot"` path for pages whose meaning is visual
