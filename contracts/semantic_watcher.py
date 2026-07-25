# v0.2.18
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *

import json
import typing
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Severity ladder. Kept as plain ints so downstream contracts can compare
# without importing anything from this module.
SEV_NONE = 0      # claim set is byte-identical after canonicalization
SEV_COSMETIC = 1  # rewording, reordering, formatting; meaning preserved
SEV_MINOR = 2     # a real but low-impact change
SEV_MATERIAL = 3  # changes the meaning of something the policy cares about
SEV_CRITICAL = 4  # reverses, removes or invalidates a policy-relevant claim

MAX_SEVERITY = 4

# Structural caps. Storage on-chain is not free and unbounded growth turns a
# cheap poke() into an unpayable one.
MAX_CLAIMS = 64
MAX_HISTORY = 32
MAX_SUBSCRIBERS = 32
MAX_KEY_LEN = 64
MAX_VALUE_LEN = 512
MAX_POLICY_LEN = 2048
MAX_URL_LEN = 512
MAX_PAGE_CHARS = 24000

# A watch that keeps failing is degraded rather than silently trusted.
FAILURE_DEGRADE_THRESHOLD = 3

# Deterministic error classes. Prefixes are stable so callers can branch on
# them without parsing prose.
ERR_EXPECTED = "EXPECTED"    # caller did something invalid
ERR_EXTERNAL = "EXTERNAL"    # the watched site misbehaved
ERR_TRANSIENT = "TRANSIENT"  # retry may succeed
ERR_LLM = "LLM_ERROR"        # model returned something unusable


# ---------------------------------------------------------------------------
# Storage types
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class Claim:
    """One canonical, policy-relevant assertion extracted from the page."""

    key: str
    value: str


@allow_storage
@dataclass
class ChangeRecord:
    """An accepted, consensus-agreed material change."""

    version: u32
    observed_at: str
    severity: u8
    summary: str
    diff_json: str


@allow_storage
@dataclass
class Subscription:
    """A subscriber and the severity floor *they* chose.

    The threshold lives with the subscriber rather than the watch so that the
    watch owner cannot decide, after the fact, what a subscriber is allowed to
    hear about.
    """

    subscriber: Address
    min_severity: u8


@allow_storage
@dataclass
class Watch:
    owner: Address
    url: str
    policy: str
    render_mode: str
    wait_after_loaded: str
    min_severity: u8
    cooldown_seconds: u64
    active: bool

    version: u32
    claims_digest: str
    last_polled_at: str
    last_change_at: str
    consecutive_failures: u32
    total_polls: u32

    claims: DynArray[Claim]
    history: DynArray[ChangeRecord]
    subscribers: DynArray[Subscription]


# ---------------------------------------------------------------------------
# Subscriber interface
# ---------------------------------------------------------------------------


@gl.contract_interface
class IWatchSubscriber:
    """Implement this to receive change callbacks.

    The callback is emitted on finality, so a subscriber is never woken by a
    change that later gets reorganised away.
    """

    class View:
        pass

    class Write:
        def on_watch_change(
            self,
            watch_id: u256,
            version: u32,
            severity: u8,
            summary: str,
            diff_json: str,
        ) -> None: ...


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class WatchCreated(gl.Event):
    def __init__(self, watch_id: u256, owner: Address, url: str, /, **blob): ...


class WatchPolled(gl.Event):
    def __init__(self, watch_id: u256, /, **blob): ...


class MaterialChange(gl.Event):
    def __init__(self, watch_id: u256, severity: u8, /, **blob): ...


class WatchDegraded(gl.Event):
    def __init__(self, watch_id: u256, /, **blob): ...


class WatchActiveChanged(gl.Event):
    """Emitted whenever observation is paused or resumed.

    Pausing is the one owner power that can still starve a subscriber of
    events, so it is made loudly visible rather than silent.
    """

    def __init__(self, watch_id: u256, active: bool, /, **blob): ...


class WatchSensitivityChanged(gl.Event):
    def __init__(self, watch_id: u256, min_severity: u8, /, **blob): ...


# ---------------------------------------------------------------------------
# Prompt construction
#
# Kept as module-level pure functions: they are easy to unit test in isolation
# and they keep the contract body readable.
# ---------------------------------------------------------------------------


def build_extraction_prompt(
    page_text: str, policy: str, anchors: list[dict]
) -> str:
    """Prompt for round 1: page -> canonical claim set.

    ``anchors`` is the previously agreed claim set, and it is what makes
    independent validators converge. Both halves matter, and measurements on
    live consensus showed why:

    * Anchoring **keys** stops each node inventing its own naming for the same
      claim (``refund_window`` vs ``refund_period`` vs ``return_window``).
    * Anchoring **values** stops equivalent phrasings drifting on every poll
      ("no permission needed" -> "none"). Without it the snapshot digest
      changes even when the page has not, which defeats the deterministic gate
      and forces a classification round on every single poll.
    """

    if len(anchors) > 0:
        anchor_block = (
            "ESTABLISHED CLAIMS from the last agreed snapshot:\n"
            + "\n".join(
                f'- {a["key"]}: {a["value"]}' for a in anchors
            )
            + "\n\nANCHORING RULES -- these take priority over style:\n"
            "a. Reuse an established key verbatim whenever that claim is still "
            "present, even if the page has reworded it.\n"
            "b. If an established claim is still true in substance, reproduce "
            "its previous value VERBATIM. Do not rephrase, expand, abbreviate "
            "or re-normalise wording you would otherwise have written "
            "differently. Character-for-character reuse is required.\n"
            "c. Only write a new value when the substance itself has changed. "
            "A different value is a signal that something really moved, so it "
            "must never be caused by style alone."
        )
    else:
        anchor_block = (
            "There are no established claims yet. Invent stable, descriptive "
            "snake_case keys and concise values that will still make sense on "
            "future revisions of this page."
        )

    return f"""You extract a canonical, machine-comparable snapshot of a web page.

POLICY -- only extract claims relevant to this concern:
{policy}

{anchor_block}

RULES
1. Follow the anchoring rules above before anything else. Stability across
   polls matters more than the wording you would otherwise prefer.
2. If an established claim has disappeared from the page, omit it. Do not
   guess and do not carry it forward.
3. Normalise values: strip marketing language, collapse whitespace, use plain
   digits for numbers, use ISO-8601 for dates. Record the substance, not the
   phrasing.
4. Ignore anything that is not policy-relevant: navigation, ads, cookie
   banners, social links, session identifiers, view counters, "last updated"
   stamps, and any other content that changes on every request.
5. At most {MAX_CLAIMS} claims. Keys under {MAX_KEY_LEN} characters, values
   under {MAX_VALUE_LEN} characters.
6. Order claims alphabetically by key.

Return ONLY this JSON object, no prose and no code fences:
{{"claims": [{{"key": "...", "value": "..."}}]}}

PAGE CONTENT
------------
{page_text}
"""


def build_diff_prompt(
    policy: str, before: list[dict], after: list[dict]
) -> str:
    """Prompt for round 2: two claim sets -> severity verdict."""

    return f"""You classify how significant a change to a web page is.

POLICY -- the concern that defines what "significant" means here:
{policy}

PREVIOUS SNAPSHOT
{json.dumps(before, sort_keys=True)}

CURRENT SNAPSHOT
{json.dumps(after, sort_keys=True)}

SEVERITY SCALE
{SEV_COSMETIC} = cosmetic. Rewording, reordering or reformatting only. Meaning
    is unchanged. A reader acting on the old snapshot would not be misled.
{SEV_MINOR} = minor. A real change, but it does not affect any decision the
    policy is concerned with.
{SEV_MATERIAL} = material. The meaning of a policy-relevant claim changed. A
    reader relying on the old snapshot could now make a wrong decision.
{SEV_CRITICAL} = critical. A policy-relevant claim was reversed, removed, or
    invalidated outright.

RULES
1. Judge substance, not wording. Identical meaning expressed differently is
   {SEV_COSMETIC}, always.
2. A key being renamed while its meaning is unchanged is {SEV_COSMETIC}.
3. Weigh every change against the policy. A large edit to something the policy
   does not care about is still {SEV_MINOR} at most.
4. Report the single highest severity across all changes.
5. List each substantive change with its key and its before/after values. Use
   null where a claim was added or removed.
6. Keep the summary under 200 characters and factual.

Return ONLY this JSON object, no prose and no code fences:
{{"severity": <int>, "summary": "...", "changes": [
  {{"key": "...", "before": "..." | null, "after": "..." | null, "why": "..."}}
]}}
"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def canonical_claims_digest(claims: list[dict]) -> str:
    """Order-independent, formatting-independent digest of a claim set.

    Computed deterministically outside every non-deterministic block, so the
    "did anything change at all?" decision costs nothing and cannot diverge
    between nodes.
    """

    normalised = sorted(
        (str(c.get("key", "")), str(c.get("value", ""))) for c in claims
    )
    payload = json.dumps(normalised, separators=(",", ":"), ensure_ascii=False)
    return Keccak256(payload.encode("utf-8")).hexdigest()


def parse_json_envelope(raw: typing.Any) -> dict:
    """Defensively turn model output into a dict.

    Models wrap JSON in code fences and prose more often than anyone would
    like. We recover the outermost object rather than failing the whole
    transaction over punctuation.

    Already-decoded input is accepted too: some model backends hand back a
    parsed object rather than a string, and that should not be an error path.
    """

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"{ERR_LLM}: model output was not text or an object")

    text = raw.strip()

    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    raise ValueError(f"{ERR_LLM}: model output was not a JSON object")


def sanitise_claims(raw_claims: typing.Any) -> list[dict]:
    """Coerce model-supplied claims into a bounded, well-typed, sorted list."""

    if not isinstance(raw_claims, list):
        raise ValueError(f"{ERR_LLM}: 'claims' was not a list")

    cleaned: dict[str, str] = {}
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()[:MAX_KEY_LEN]
        if key == "":
            continue
        value = " ".join(str(item.get("value", "")).split())[:MAX_VALUE_LEN]
        # Later duplicates lose; keys are unique by construction.
        if key not in cleaned:
            cleaned[key] = value

    ordered = sorted(cleaned.items())[:MAX_CLAIMS]
    return [{"key": k, "value": v} for k, v in ordered]


def current_datetime() -> str:
    """The transaction timestamp, as an ISO-8601 string.

    The SDK exposes this on the raw message object. Direct-mode test
    harnesses build a reduced message object and expose the same field via the
    ``gl.message_raw`` mapping instead, so we accept either shape rather than
    letting the contract behave differently under test than in production.
    """

    message = getattr(gl, "message", None)
    raw = getattr(message, "raw", None)
    value = getattr(raw, "datetime", None)
    if isinstance(value, str) and value != "":
        return value

    mapping = getattr(gl, "message_raw", None)
    if isinstance(mapping, dict):
        fallback = mapping.get("datetime")
        if isinstance(fallback, str) and fallback != "":
            return fallback

    return ""


def clamp_severity(raw: typing.Any) -> int:
    """Never trust a model to stay inside its own scale."""

    try:
        value = int(raw)
    except Exception:
        # Unparseable severity is treated as material: the safe direction is
        # to over-report, never to silently swallow a change.
        return SEV_MATERIAL
    if value < SEV_NONE:
        return SEV_NONE
    if value > MAX_SEVERITY:
        return MAX_SEVERITY
    return value


# ---------------------------------------------------------------------------
# Envelope packing
#
# Every non-deterministic round returns a JSON envelope rather than raising, so
# that a failure is something validators can *agree on* instead of an execution
# abort. These packers are pure functions of the model's raw output, which
# makes the interesting behaviour -- malformed JSON, out-of-range severities,
# duplicate keys -- unit-testable without a node.
# ---------------------------------------------------------------------------


def pack_error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, sort_keys=True)


def pack_observation(raw_model_output: str) -> str:
    """Round 1 envelope: raw model output -> canonical claim set."""

    try:
        claims = sanitise_claims(
            parse_json_envelope(raw_model_output).get("claims")
        )
    except Exception as exc:
        return pack_error(f"{ERR_LLM}: {exc}")

    return json.dumps({"ok": True, "claims": claims}, sort_keys=True)


def pack_verdict(raw_model_output: str) -> str:
    """Round 2 envelope: raw model output -> clamped severity verdict."""

    try:
        parsed = parse_json_envelope(raw_model_output)
    except Exception as exc:
        return pack_error(f"{ERR_LLM}: {exc}")

    changes = parsed.get("changes")
    if not isinstance(changes, list):
        changes = []

    return json.dumps(
        {
            "ok": True,
            "severity": clamp_severity(parsed.get("severity")),
            "summary": " ".join(str(parsed.get("summary", "")).split())[:200],
            "changes": changes[:MAX_CLAIMS],
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Equivalence principles
#
# Written out here so the reasoning is reviewable in one place rather than
# buried inline.
# ---------------------------------------------------------------------------

# Round 1 cannot use strict equality: two validators rendering the same page
# seconds apart legitimately see different bytes. What must agree is the
# *extracted meaning*.
EQ_OBSERVE = (
    "Both outputs are canonical snapshots of the same web page under the same "
    "policy. They are equivalent if they carry the same information: the same "
    "set of policy-relevant claims, with values that mean the same thing. "
    "Ignore differences in key naming, ordering, whitespace, punctuation, "
    "casing, and units that denote the same quantity. Ignore any claim derived "
    "from content that changes on every page load, such as timestamps, view "
    "counts, session identifiers or advertisements. Values differing only in "
    "phrasing are equivalent. Values differing in substance -- a different "
    "number, date, name, or a reversed statement -- are NOT equivalent. "
    "If one output reports an error and the other does not, they are NOT "
    "equivalent."
)

# Round 2 agreement is about the *verdict*, not the prose used to explain it.
EQ_JUDGE = (
    "Both outputs classify the same change between two snapshots. They are "
    "equivalent if they reach the same conclusion: the severity values must "
    "match exactly, and both must identify the same substantive changes. "
    "Differences in wording of the summary or of any explanation are "
    "irrelevant. Differences in the order of the changes list are irrelevant. "
    "A different severity value means they are NOT equivalent."
)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class SemanticWatcher(gl.Contract):
    """Registry of watched URLs and their verified change history."""

    watches: TreeMap[u256, Watch]
    next_id: u256
    admin: Address

    def __init__(self):
        self.next_id = u256(1)
        self.admin = gl.message.sender_address

    # -- internal helpers ---------------------------------------------------

    def _require_watch(self, watch_id: u256) -> Watch:
        watch = self.watches.get(watch_id)
        if watch is None:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: unknown watch {watch_id}")
        return watch

    def _require_owner(self, watch: Watch) -> None:
        if watch.owner != gl.message.sender_address:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: caller is not the watch owner")

    def _claims_to_list(self, watch: Watch) -> list[dict]:
        """Copy storage claims into plain Python before any nondet block."""
        return [{"key": str(c.key), "value": str(c.value)} for c in watch.claims]

    def _store_claims(self, watch: Watch, claims: list[dict]) -> None:
        watch.claims.clear()
        for c in claims:
            watch.claims.append(Claim(key=c["key"], value=c["value"]))

    def _append_history(self, watch: Watch, record: ChangeRecord) -> None:
        """Append with a ring-buffer cap so history cannot grow without bound."""
        if len(watch.history) >= MAX_HISTORY:
            retained = [watch.history[i] for i in range(1, len(watch.history))]
            watch.history.clear()
            for item in retained:
                watch.history.append(item)
        watch.history.append(record)

    def _parse_ts(self, value: str) -> int:
        """Seconds since epoch from an ISO-8601 transaction timestamp.

        Returns 0 when unparseable, which makes the cooldown check fail open --
        deliberately: a watch that cannot be polled is worse than one polled
        slightly too often.
        """
        import datetime

        try:
            return int(
                datetime.datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).timestamp()
            )
        except Exception:
            return 0

    # -- consensus rounds ---------------------------------------------------
    #
    # Each of these is a thin wrapper holding exactly one equivalence-principle
    # block and nothing else. Keeping them isolated means no storage write,
    # message emission or nested non-deterministic call can ever end up inside
    # a consensus block by accident.
    #
    # These two methods contain the ONLY non-determinism in the contract --
    # three operations in total, each of which has no deterministic form:
    #
    #   gl.nondet.web.render      network I/O. Two nodes fetching the same URL
    #                             milliseconds apart legitimately receive
    #                             different bytes. The alternative to fetching
    #                             is not "fetch deterministically", it is "have
    #                             someone tell you and trust them".
    #
    #   exec_prompt (extract)     reducing prose to canonical claims is a
    #                             language-understanding task. A deterministic
    #                             parser can pull a <div>; it cannot see that
    #                             "returns within one month" and "30-day refund
    #                             window" are the same claim, which is the
    #                             entire point.
    #
    #   exec_prompt (classify)    "did the meaning change, and does it matter
    #                             under this policy" is irreducibly a
    #                             judgement. There is no total function from
    #                             two strings to a severity.
    #
    # Everything else is deterministic on purpose: the digest gate that decides
    # whether anything changed at all, access control, cooldown arithmetic, the
    # severity comparison, storage, events and subscriber fan-out.
    #
    # The shape to notice is that the model is asked what the page *says*, and
    # never what the contract should *do*. Every state transition and every
    # payout-adjacent decision is deterministic code acting on an observation
    # the validator set has already agreed on.

    def _observe(
        self,
        url: str,
        policy: str,
        anchors: list[dict],
        render_mode: str,
        wait_after_loaded: str,
    ) -> dict:
        """Round 1: fetch the page and canonicalise it, under consensus."""

        def leader() -> str:
            try:
                page = gl.nondet.web.render(
                    url,
                    mode=render_mode,  # type: ignore[arg-type]
                    wait_after_loaded=(
                        wait_after_loaded if wait_after_loaded else None
                    ),
                )
            except Exception as exc:
                return pack_error(f"{ERR_EXTERNAL}: fetch failed: {exc}")

            page_text = str(page)
            if len(page_text.strip()) == 0:
                return pack_error(f"{ERR_EXTERNAL}: page rendered empty")

            try:
                raw = gl.nondet.exec_prompt(
                    build_extraction_prompt(
                        page_text[:MAX_PAGE_CHARS], policy, anchors
                    ),
                    response_format="text",
                )
            except Exception as exc:
                return pack_error(f"{ERR_TRANSIENT}: model call failed: {exc}")

            return pack_observation(raw)

        return json.loads(gl.eq_principle.prompt_comparative(leader, EQ_OBSERVE))

    def _judge(self, policy: str, before: list[dict], after: list[dict]) -> dict:
        """Round 2: classify the significance of the diff, under consensus."""

        def leader() -> str:
            try:
                raw = gl.nondet.exec_prompt(
                    build_diff_prompt(policy, before, after),
                    response_format="text",
                )
            except Exception as exc:
                return pack_error(f"{ERR_TRANSIENT}: model call failed: {exc}")

            return pack_verdict(raw)

        return json.loads(gl.eq_principle.prompt_comparative(leader, EQ_JUDGE))

    def _cooldown_remaining(self, watch: Watch, now: str) -> int:
        """Seconds left before this watch may be polled again.

        Fails open -- returns 0 -- only when a timestamp is genuinely
        unreadable. A watch that can never be polled because of a formatting
        quirk is a worse outcome than one polled too eagerly. Note that an
        elapsed time of exactly zero is *not* the fail-open case: two pokes in
        the same second must still respect a non-zero cooldown.
        """

        now_ts = self._parse_ts(now)
        last_ts = self._parse_ts(str(watch.last_polled_at))
        if now_ts <= 0 or last_ts <= 0:
            return 0

        remaining = int(watch.cooldown_seconds) - (now_ts - last_ts)
        return remaining if remaining > 0 else 0

    def _notify(self, watch: Watch, watch_id: u256, record: ChangeRecord) -> None:
        """Deliver a change to every subscriber whose own floor it clears."""
        for entry in watch.subscribers:
            if int(record.severity) < int(entry.min_severity):
                continue
            IWatchSubscriber(entry.subscriber).emit(
                on="finalized"
            ).on_watch_change(
                watch_id,
                record.version,
                record.severity,
                str(record.summary),
                str(record.diff_json),
            )

    # -- lifecycle ----------------------------------------------------------

    @gl.public.write
    def create_watch(
        self,
        url: str,
        policy: str,
        min_severity: int = SEV_MATERIAL,
        cooldown_seconds: int = 3600,
        render_mode: str = "text",
        wait_after_loaded: str = "",
    ) -> u256:
        """Register a URL and take its baseline snapshot.

        The baseline costs one full observation round. It is deliberately part
        of creation: a watch without a baseline would report its first poll as
        a total rewrite of the page.
        """

        if len(url) == 0 or len(url) > MAX_URL_LEN:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: url must be 1..{MAX_URL_LEN} chars")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise gl.vm.UserError(f"{ERR_EXPECTED}: url must be http(s)")
        if len(policy) == 0 or len(policy) > MAX_POLICY_LEN:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: policy must be 1..{MAX_POLICY_LEN} chars"
            )
        if render_mode not in ("text", "html"):
            raise gl.vm.UserError(f"{ERR_EXPECTED}: render_mode must be 'text' or 'html'")
        if min_severity < SEV_COSMETIC or min_severity > MAX_SEVERITY:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: min_severity must be {SEV_COSMETIC}..{MAX_SEVERITY}"
            )
        if cooldown_seconds < 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: cooldown_seconds must be >= 0")

        result = self._observe(url, policy, [], render_mode, wait_after_loaded)
        if not result.get("ok", False):
            raise gl.vm.UserError(str(result.get("error", f"{ERR_EXTERNAL}: baseline failed")))

        claims = result["claims"]
        now = current_datetime()
        watch_id = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        # The Watch is allocated directly in storage and then populated.
        # DynArray cannot be constructed in memory, so building a Watch value
        # and assigning it afterwards is not an option -- its collection fields
        # only exist once the slot does.
        watch = self.watches.get_or_insert_default(watch_id)
        watch.owner = gl.message.sender_address
        watch.url = url
        watch.policy = policy
        watch.render_mode = render_mode
        watch.wait_after_loaded = wait_after_loaded
        watch.min_severity = u8(min_severity)
        watch.cooldown_seconds = u64(cooldown_seconds)
        watch.active = True
        watch.version = u32(1)
        watch.claims_digest = canonical_claims_digest(claims)
        watch.last_polled_at = now
        watch.last_change_at = now
        watch.consecutive_failures = u32(0)
        watch.total_polls = u32(1)
        self._store_claims(watch, claims)

        WatchCreated(watch_id, gl.message.sender_address, url).emit()
        return watch_id

    @gl.public.write
    def poke(self, watch_id: u256) -> None:
        """Re-observe a watch and record any material change.

        Permissionless by design -- anyone can pay to advance a watch. The
        cooldown, not an access check, is what bounds the cost.
        """

        watch = self._require_watch(watch_id)
        if not watch.active:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: watch {watch_id} is paused")

        now = current_datetime()
        remaining = self._cooldown_remaining(watch, now)
        if remaining > 0:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: cooldown active, {remaining}s remaining"
            )

        # Copy everything the nondet closure needs into plain locals.
        url = str(watch.url)
        policy = str(watch.policy)
        render_mode = str(watch.render_mode)
        wait_after_loaded = str(watch.wait_after_loaded)
        before = self._claims_to_list(watch)

        # --- round 1: fetch + canonicalise ---------------------------------
        # The previous snapshot is fed back in as the anchor, keys and values
        # both, so an unchanged page reproduces an identical claim set.
        observed = self._observe(
            url, policy, before, render_mode, wait_after_loaded
        )

        watch.total_polls = u32(int(watch.total_polls) + 1)
        watch.last_polled_at = now

        if not observed.get("ok", False):
            # A site being down is not a content change. Record and back off.
            watch.consecutive_failures = u32(int(watch.consecutive_failures) + 1)
            if int(watch.consecutive_failures) == FAILURE_DEGRADE_THRESHOLD:
                WatchDegraded(
                    watch_id, reason=str(observed.get("error", "unknown"))
                ).emit()
            WatchPolled(watch_id, changed=False, ok=False).emit()
            return

        watch.consecutive_failures = u32(0)
        after = observed["claims"]

        # --- deterministic gate --------------------------------------------
        digest = canonical_claims_digest(after)
        if digest == str(watch.claims_digest):
            WatchPolled(watch_id, changed=False, ok=True).emit()
            return

        # --- round 2: classify ---------------------------------------------
        verdict = self._judge(policy, before, after)
        if not verdict.get("ok", False):
            # We know something moved but cannot classify it. Keep the old
            # snapshot so the change is re-detected on the next poke rather
            # than being lost.
            WatchPolled(
                watch_id, changed=True, ok=False, error=str(verdict.get("error", ""))
            ).emit()
            return

        severity = int(verdict["severity"])

        # The snapshot advances regardless of severity: cosmetic drift must be
        # absorbed, otherwise every later diff is measured against stale text.
        self._store_claims(watch, after)
        watch.claims_digest = digest

        if severity < int(watch.min_severity):
            WatchPolled(watch_id, changed=True, ok=True, severity=severity).emit()
            return

        watch.version = u32(int(watch.version) + 1)
        watch.last_change_at = now

        record = ChangeRecord(
            version=watch.version,
            observed_at=now,
            severity=u8(severity),
            summary=str(verdict.get("summary", "")),
            diff_json=json.dumps(verdict.get("changes", []), sort_keys=True),
        )
        self._append_history(watch, record)

        MaterialChange(watch_id, u8(severity), version=int(watch.version)).emit()
        self._notify(watch, watch_id, record)

    # -- subscriptions ------------------------------------------------------

    @gl.public.write
    def subscribe(self, watch_id: u256, min_severity: int = SEV_MATERIAL) -> None:
        """Register the caller for change callbacks at a severity they choose.

        The floor belongs to the subscriber. A watch owner can make the watch
        more sensitive over time but can never raise a subscriber's threshold,
        so the notifications you sign up for are the ones you keep getting.
        """

        if min_severity < SEV_COSMETIC or min_severity > MAX_SEVERITY:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: min_severity must be {SEV_COSMETIC}..{MAX_SEVERITY}"
            )

        watch = self._require_watch(watch_id)
        if len(watch.subscribers) >= MAX_SUBSCRIBERS:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: subscriber limit reached")

        caller = gl.message.sender_address
        for existing in watch.subscribers:
            if existing.subscriber == caller:
                raise gl.vm.UserError(f"{ERR_EXPECTED}: already subscribed")

        entry = watch.subscribers.append_new_get()
        entry.subscriber = caller
        entry.min_severity = u8(min_severity)

    @gl.public.write
    def unsubscribe(self, watch_id: u256) -> None:
        watch = self._require_watch(watch_id)
        caller = gl.message.sender_address

        retained = [
            (e.subscriber, int(e.min_severity))
            for e in watch.subscribers
            if e.subscriber != caller
        ]
        if len(retained) == len(watch.subscribers):
            raise gl.vm.UserError(f"{ERR_EXPECTED}: not subscribed")

        watch.subscribers.clear()
        for address, floor in retained:
            entry = watch.subscribers.append_new_get()
            entry.subscriber = address
            entry.min_severity = u8(floor)

    # -- owner controls -----------------------------------------------------

    # Owner controls follow one rule: they may only ever make a watch *more*
    # responsive. Anything a subscriber relies on must be impossible to walk
    # back, otherwise the owner of a watched page could quietly mute reports
    # about their own page after others have committed to it.
    #
    # url and policy are immutable by construction -- there is deliberately no
    # setter for either. Pausing is the one remaining lever that can starve a
    # subscriber, so it stays available but is made loud and observable.

    @gl.public.write
    def set_active(self, watch_id: u256, active: bool) -> None:
        """Pause or resume observation.

        Pausing cannot be silent: it emits an event and flips ``reliable`` to
        false in ``get_watch``. Subscribers must treat a paused watch the same
        way they treat a degraded one -- as an absence of information, not as
        evidence that nothing changed.
        """
        watch = self._require_watch(watch_id)
        self._require_owner(watch)
        if bool(watch.active) == active:
            return
        watch.active = active
        WatchActiveChanged(watch_id, active).emit()

    @gl.public.write
    def set_min_severity(self, watch_id: u256, min_severity: int) -> None:
        """Lower the recording threshold. Raising it is refused.

        Raising would retroactively suppress changes that existing subscribers
        subscribed in order to hear about. Owners who want a narrower feed
        should create a second watch; subscribers pick their own floor when
        they subscribe.
        """
        if min_severity < SEV_COSMETIC or min_severity > MAX_SEVERITY:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: min_severity must be {SEV_COSMETIC}..{MAX_SEVERITY}"
            )
        watch = self._require_watch(watch_id)
        self._require_owner(watch)

        if min_severity > int(watch.min_severity):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: min_severity may only be lowered "
                f"(current {int(watch.min_severity)}, requested {min_severity})"
            )
        if min_severity == int(watch.min_severity):
            return

        watch.min_severity = u8(min_severity)
        WatchSensitivityChanged(watch_id, u8(min_severity)).emit()

    @gl.public.write
    def set_cooldown(self, watch_id: u256, cooldown_seconds: int) -> None:
        """Shorten the polling interval. Lengthening it is refused.

        A long enough cooldown is indistinguishable from pausing the watch, so
        it is constrained the same way as the severity threshold.
        """
        if cooldown_seconds < 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: cooldown_seconds must be >= 0")
        watch = self._require_watch(watch_id)
        self._require_owner(watch)

        if cooldown_seconds > int(watch.cooldown_seconds):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: cooldown_seconds may only be lowered "
                f"(current {int(watch.cooldown_seconds)}, requested {cooldown_seconds})"
            )
        watch.cooldown_seconds = u64(cooldown_seconds)

    @gl.public.write
    def transfer_watch(self, watch_id: u256, new_owner: Address) -> None:
        """Hand a watch to a new owner.

        The parameter is annotated ``Address``, but calldata off the wire
        arrives as a hex string and is *not* coerced before it reaches
        storage -- assigning it directly raises
        ``AttributeError: 'str' object has no attribute 'as_bytes'`` on a real
        network while passing happily in direct-mode tests that construct an
        Address by hand. Normalise here rather than trusting the annotation.
        """
        watch = self._require_watch(watch_id)
        self._require_owner(watch)

        try:
            owner = new_owner if isinstance(new_owner, Address) else Address(new_owner)
        except Exception as exc:
            raise gl.vm.UserError(f"{ERR_EXPECTED}: invalid new_owner: {exc}")

        # Address.ZERO is documented but absent in SDK v0.2.16, so compare the
        # raw bytes rather than depending on the constant.
        if bytes(owner.as_bytes) == b"\x00" * Address.SIZE:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED}: refusing to transfer a watch to the zero address"
            )

        watch.owner = owner

    # -- views --------------------------------------------------------------

    @gl.public.view
    def get_watch(self, watch_id: u256) -> dict:
        watch = self._require_watch(watch_id)
        degraded = int(watch.consecutive_failures) >= FAILURE_DEGRADE_THRESHOLD
        return {
            "owner": str(watch.owner),
            "url": str(watch.url),
            "policy": str(watch.policy),
            "render_mode": str(watch.render_mode),
            "min_severity": int(watch.min_severity),
            "cooldown_seconds": int(watch.cooldown_seconds),
            "active": bool(watch.active),
            "version": int(watch.version),
            "claims_digest": str(watch.claims_digest),
            "last_polled_at": str(watch.last_polled_at),
            "last_change_at": str(watch.last_change_at),
            "consecutive_failures": int(watch.consecutive_failures),
            "total_polls": int(watch.total_polls),
            "claim_count": len(watch.claims),
            "subscriber_count": len(watch.subscribers),
            "degraded": degraded,
            # One flag consumers should gate on. Silence from an unreliable
            # watch means "we do not know", never "nothing changed".
            "reliable": bool(watch.active) and not degraded,
        }

    @gl.public.view
    def get_claims(self, watch_id: u256) -> list:
        watch = self._require_watch(watch_id)
        return self._claims_to_list(watch)

    @gl.public.view
    def get_history(self, watch_id: u256) -> list:
        watch = self._require_watch(watch_id)
        return [
            {
                "version": int(r.version),
                "observed_at": str(r.observed_at),
                "severity": int(r.severity),
                "summary": str(r.summary),
                "changes": json.loads(str(r.diff_json)),
            }
            for r in watch.history
        ]

    @gl.public.view
    def get_latest_change(self, watch_id: u256) -> dict | None:
        watch = self._require_watch(watch_id)
        if len(watch.history) == 0:
            return None
        record = watch.history[len(watch.history) - 1]
        return {
            "version": int(record.version),
            "observed_at": str(record.observed_at),
            "severity": int(record.severity),
            "summary": str(record.summary),
            "changes": json.loads(str(record.diff_json)),
        }

    @gl.public.view
    def get_subscribers(self, watch_id: u256) -> list:
        watch = self._require_watch(watch_id)
        return [
            {
                "subscriber": str(e.subscriber),
                "min_severity": int(e.min_severity),
            }
            for e in watch.subscribers
        ]

    @gl.public.view
    def is_due(self, watch_id: u256) -> bool:
        """Whether poke() would pass its cooldown check right now."""
        watch = self._require_watch(watch_id)
        if not watch.active:
            return False
        return self._cooldown_remaining(watch, current_datetime()) == 0

    @gl.public.view
    def watch_count(self) -> int:
        return int(self.next_id) - 1
