"""`fujimoto broadcast follow ID`: what became of one targeted message.

After `fujimoto send` (or MCP `send_message`) the sender has an id. This
answers, within a bounded wait, four separate questions, each from its own
evidence and never one inferred from another:

- **delivery**: the target's waiter receipt (delivered / rejected), or
  none yet; plus whether the target is listening right now.
- **approval** (only when the message carries one): whether it *would*
  verify for the target, re-checked from the mailbox here; and, separately,
  whether the target's transcript shows it running `fujimoto broadcast
  verify ID` and what that printed. A passing re-check here is not evidence
  that the recipient checked: that needs the transcript.
- **reply**: the assistant text that followed the delivery
  (`load_log(reply_to=ID)`), and the transcript `cursor` to read on from.
- **target**: the session's current activity, including a wait on the user
  (a permission prompt, a question).

Read-only: it reads the mailbox, receipts, transcripts and Claude's
registry, and never signs, sends or writes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from fujimoto import broadcast, debug, session_query
from fujimoto.claude.log_parser import TranscriptMessage

POLL_INTERVAL = 1.0

# delivery.state
PENDING, DELIVERED, REJECTED, EXPIRED = "pending", "delivered", "rejected", "expired"
# reply.state
NONE, PARTIAL, FINAL, UNKNOWN = "none", "partial", "final", "unknown"
# approval.recipient_check
NOT_SEEN, RAN, PASSED, FAILED = "not_seen", "ran", "passed", "failed"
# outcome: why the wait ended
OUTCOMES = (
    "replied",
    "no_reply",
    "rejected",
    "expired",
    "blocked",
    "stopped",
    "timeout",
)


@dataclass(frozen=True)
class Delivery:
    state: str
    reason: str | None = None
    at: str | None = None
    # The target has a live waiter now (it is briefly absent while handling
    # a message).
    listening: bool = False


@dataclass(frozen=True)
class RecipientCheck:
    """What the target's own transcript shows about `broadcast verify ID`."""

    state: str  # not_seen | ran (no result yet / unclear) | passed | failed
    index: int | None = None


@dataclass(frozen=True)
class Approval:
    text: str
    # Would `broadcast verify` pass for the target now, re-checked here
    # against this machine's current signers. Not the recipient's check.
    verifiable: bool
    failures: tuple[str, ...]
    recipient_check: RecipientCheck


@dataclass(frozen=True)
class Reply:
    state: str  # none | partial | final | unknown
    delivered_index: int | None
    messages: tuple[tuple[int, TranscriptMessage], ...]
    # The transcript's last index: `fujimoto log SESSION --after CURSOR`.
    cursor: int | None
    note: str | None = None


@dataclass(frozen=True)
class FollowReport:
    id: str
    entry: str
    target_session: str
    signer: str | None
    expires: datetime
    expired: bool
    delivery: Delivery
    approval: Approval | None
    reply: Reply
    target: session_query.Activity
    outcome: str | None  # None while the wait is still open
    waited: float = 0.0


# -- One look ---------------------------------------------------------------


def _verify_marker(message_id: str) -> re.Pattern[str]:
    return re.compile(rf"\bbroadcast\s+verify\s+{re.escape(message_id)}(?![\w-])")


def recipient_check(
    messages: list[tuple[int, TranscriptMessage]], message_id: str, target: str
) -> RecipientCheck:
    """The target's own `fujimoto broadcast verify ID` run, from its transcript.

    Evidence only: a tool call whose command names the id, paired by tool id
    with its result. `passed` needs the result to say PASS *for the target
    session* (text or `--json`), so a check run with `--session` for some
    other id doesn't count. The last such call wins.
    """
    marker = _verify_marker(message_id)
    results = {m.tool_id: m for _, m in messages if m.role == "tool_result"}
    found = RecipientCheck(NOT_SEEN)
    for index, m in messages:
        if m.role != "tool_use" or not marker.search(m.text):
            continue
        result = results.get(m.tool_id) if m.tool_id else None
        text = result.text if result else ""
        passed = (
            f"PASS  {message_id}:" in text and f"for session {target[:8]}" in text
        ) or (
            re.search(r'"passed":\s*true', text) is not None
            and f'"session_id": "{target}"' in text
        )
        failed = f"FAIL  {message_id}:" in text or re.search(r'"passed":\s*false', text)
        state = PASSED if passed else FAILED if failed else RAN
        found = RecipientCheck(state, index)
    return found


def _reply_state(
    span: list[tuple[int, TranscriptMessage]], activity: session_query.Activity
) -> str:
    if not any(m.role == "assistant" for _, m in span):
        return NONE
    # Final only on evidence the turn is over: stopped, or the registry says idle.
    if not activity.running or activity.status == "idle":
        return FINAL
    return PARTIAL


def _outcome(report: FollowReport) -> str | None:
    """Why to stop waiting now, or None to keep waiting."""
    if report.delivery.state == REJECTED:
        return "rejected"
    if report.delivery.state == EXPIRED:
        return "expired"
    if report.reply.state == FINAL:
        return "replied"
    if report.target.running and report.target.waiting_for:
        return "blocked"
    delivered = (
        report.delivery.state == DELIVERED or report.reply.delivered_index is not None
    )
    if not report.target.running and not delivered:
        return "stopped"
    if (
        report.reply.delivered_index is not None
        and report.reply.state == NONE
        and report.target.running
        and report.target.status == "idle"
        and report.reply.cursor is not None
        and report.reply.cursor > report.reply.delivered_index
    ):
        # The turn after the delivery ended without any text.
        return "no_reply"
    return None


def look(message_id: str, *, now: datetime | None = None) -> FollowReport:
    """One snapshot of a targeted message, with no waiting."""
    entry = broadcast.find_entry(message_id)
    payload = broadcast.read_payload(entry)
    if payload.session is None:
        raise broadcast.BroadcastError(
            f"{message_id} is a broadcast, not a message to one session; "
            "see `fujimoto broadcast status`"
        )
    target = payload.session
    now = now or broadcast._now()
    expired = now >= payload.expires

    receipt = next(
        (r for r in broadcast.read_receipts(entry.name) if r.session_id == target),
        None,
    )
    listening = target in broadcast.listening_session_ids()
    if receipt is not None:
        delivery = Delivery(receipt.outcome, receipt.reason, receipt.at, listening)
    else:
        delivery = Delivery(EXPIRED if expired else PENDING, None, None, listening)

    try:
        log = session_query.load_log(target, reply_to=message_id, roles=["assistant"])
    except session_query.QueryError as e:
        cwd = Path(receipt.cwd) if receipt and receipt.cwd else Path("/")
        activity = session_query.ActivityIndex.gather().for_session(target, cwd)
        reply = Reply(UNKNOWN, None, (), None, note=str(e))
        log = None
    else:
        activity = log.activity
        span = list(log.messages or ())
        reply = Reply(
            _reply_state(span, activity),
            log.delivered_index,
            tuple(span),
            log.cursor,
            note=log.parse_error,
        )

    approval = None
    signer = receipt.signer if receipt else None
    if payload.approval is not None:
        verdict = broadcast.verify_approval(message_id, target, now=now)
        signer = signer or verdict.signer
        check = RecipientCheck(NOT_SEEN)
        if log is not None and log.delivered_index is not None:
            try:
                tools = session_query.load_log(
                    target,
                    after=log.delivered_index,
                    roles=["tool_use", "tool_result"],
                )
            except session_query.QueryError:
                pass  # vanished between reads: no evidence either way
            else:
                check = recipient_check(list(tools.messages or ()), message_id, target)
        approval = Approval(payload.approval, verdict.passed, verdict.failures, check)

    report = FollowReport(
        id=payload.id,
        entry=entry.name,
        target_session=target,
        signer=signer,
        expires=payload.expires,
        expired=expired,
        delivery=delivery,
        approval=approval,
        reply=reply,
        target=activity,
        outcome=None,
    )
    return report


def follow(
    message_id: str,
    *,
    wait: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    snapshot: Callable[[str], FollowReport] = look,
) -> FollowReport:
    """`look` every `POLL_INTERVAL` until there is an outcome or `wait` runs out.

    Stops early when the target has replied (its turn is over), the message
    was rejected or expired undelivered, the target is waiting on the user,
    or it stopped before the message arrived. Otherwise the outcome is
    `timeout` and the report is the last snapshot.
    """
    start = clock()
    deadline = start + max(0.0, wait)
    report = snapshot(message_id)
    outcome = _outcome(report)
    while outcome is None and clock() < deadline:
        sleep(POLL_INTERVAL)
        report = snapshot(message_id)
        outcome = _outcome(report)
    waited = round(clock() - start, 1)
    final = replace(report, outcome=outcome or "timeout", waited=waited)
    debug.log(
        "follow.done",
        id=message_id,
        outcome=final.outcome,
        delivery=final.delivery.state,
        reply=final.reply.state,
        approval=(
            "none"
            if final.approval is None
            else f"{final.approval.verifiable}/{final.approval.recipient_check.state}"
        ),
        waited=waited,
    )
    return final


# -- Output ------------------------------------------------------------------

_RECIPIENT_TEXT = {
    NOT_SEEN: "not seen: no `fujimoto broadcast verify` run for this id in its "
    "transcript, so no evidence the recipient verified it",
    RAN: "ran `fujimoto broadcast verify`, result not seen yet",
    PASSED: "ran `fujimoto broadcast verify`: PASS for this session",
    FAILED: "ran `fujimoto broadcast verify`: FAIL",
}

_OUTCOME_TEXT = {
    "replied": "its turn after the delivery is over",
    "no_reply": "its turn after the delivery ended without a text reply",
    "rejected": "rejected by the target",
    "expired": "expired before delivery",
    "blocked": "the target is waiting on the user",
    "stopped": "the target is not running and has not received it",
    "timeout": "still open when the wait ended",
}


def _short(session_id: str) -> str:
    return session_id[:8]


def _at(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso or "").astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso or "?"


def format_follow(report: FollowReport) -> str:
    d, r, a = report.delivery, report.reply, report.approval
    who = f", signed by {report.signer}" if report.signer else ""
    when = (
        "expired" if report.expired else f"expires {broadcast._local(report.expires)}"
    )
    lines = [
        f"Message {report.id} to session {_short(report.target_session)}{who}, {when}."
    ]
    if d.state in (DELIVERED, REJECTED):
        detail = f"{d.state} at {_at(d.at)}" + (f" ({d.reason})" if d.reason else "")
    elif d.state == EXPIRED:
        detail = "never delivered (expired)"
    else:
        detail = "no receipt yet"
    lines.append(
        f"  delivery:  {detail} · listening now: {'yes' if d.listening else 'no'}"
    )
    if a is not None:
        verdict = "yes" if a.verifiable else "no (" + "; ".join(a.failures) + ")"
        lines.append(f"  approval:  “{a.text}”")
        lines.append(f"             verifies for the target now: {verdict}")
        lines.append(
            f"             recipient's check: {_RECIPIENT_TEXT[a.recipient_check.state]}"
        )
    if r.state == UNKNOWN:
        lines.append(f"  reply:     unknown ({r.note})")
    elif r.delivered_index is None:
        lines.append("  reply:     none (not in the target's transcript yet)")
    else:
        lines.append(
            f"  reply:     {r.state} (delivered at #{r.delivered_index}, "
            f"{len(r.messages)} message{'s' if len(r.messages) != 1 else ''})"
        )
        for index, m in r.messages:
            body = m.text.replace("\n", "\n             ")
            lines.append(f"    #{index}  {body}")
    target = session_query._list_activity(report.target) or "not running"
    lines.append(f"  target:    {target}")
    lines.append(
        f"Outcome: {report.outcome} ({_OUTCOME_TEXT.get(report.outcome or '', '')}), "
        f"after {report.waited:g}s."
    )
    nxt = []
    if r.cursor is not None:
        nxt.append(
            f"fujimoto log {_short(report.target_session)} --after {r.cursor} --wait 60"
        )
    if report.outcome in ("timeout", "blocked"):
        nxt.append(f"fujimoto broadcast follow {report.id} --wait 60")
    if nxt:
        lines.append("Next: " + " · ".join(nxt))
    return "\n".join(lines)


def follow_json(report: FollowReport) -> dict[str, object]:
    d, r, a = report.delivery, report.reply, report.approval
    return {
        "id": report.id,
        "entry": report.entry,
        "target_session": report.target_session,
        "signer": report.signer,
        "expires": broadcast._iso(report.expires),
        "expired": report.expired,
        "outcome": report.outcome,
        "waited": report.waited,
        "delivery": {
            "state": d.state,
            "reason": d.reason,
            "at": d.at,
            "listening": d.listening,
        },
        "approval": (
            None
            if a is None
            else {
                "text": a.text,
                "verifiable": a.verifiable,
                "failures": list(a.failures),
                "recipient_check": {
                    "state": a.recipient_check.state,
                    "index": a.recipient_check.index,
                },
            }
        ),
        "reply": {
            "state": r.state,
            "delivered_index": r.delivered_index,
            "cursor": r.cursor,
            "note": r.note,
            "messages": [
                session_query._message_json(index, m) for index, m in r.messages
            ],
        },
        "target": report.target.as_json(),
    }
