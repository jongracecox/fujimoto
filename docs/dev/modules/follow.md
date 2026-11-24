# `follow.py`

User-facing docs: README, "Agents as senders" (`fujimoto broadcast follow`)
and the MCP tool table (`follow_message`). Why it is built this way:
[design decisions](../design-decisions.md) ("Following a message").

**`follow.py`**: `fujimoto broadcast follow ID [--wait SECS] [--json]` and
MCP `follow_message`. Read-only: mailbox, receipts, transcripts and Claude's
registry; it never signs, sends or writes.

- **`look(id)`** takes one snapshot of a *targeted* message
  (`broadcast.find_entry`, an exact id; a broadcast or an unknown id is a
  `BroadcastError`). Four parts, each from its own evidence:
  - `Delivery`: the target's own receipt (`delivered` / `rejected` +
    reason), else `pending`, or `expired` once past `expires` with no
    receipt; `listening` from `broadcast.listening_session_ids()`.
  - `Approval` (only if the payload has one): `verifiable` is
    `broadcast.verify_approval(id, target)` run *here*, against this
    machine's current signers. `recipient_check` is separate evidence from
    the target's transcript (`recipient_check()`): a `tool_use` whose text
    matches `broadcast verify <id>`, paired by `tool_id` with its result.
    `passed` needs `PASS  <id>:` and `for session <target[:8]>` (or JSON
    `"passed": true` and the target's full `session_id`), so a check run with
    `--session` for another id is only `ran`. Searched from the delivery
    onwards (`load_log(after=delivered_index, roles=tool_use,tool_result)`),
    not just the reply span, since the check can follow a user message.
  - `Reply`: `load_log(target, reply_to=id, roles=["assistant"])`, so the
    span is the assistant text after the delivery, up to the next
    user/injected message. `final` only when the registry says the target
    is idle or it is no longer running; else `partial`; `unknown` when the
    transcript can't be read (no transcript yet).
  - `target`: the `Activity` from that `load_log` (or `ActivityIndex` alone
    when there's no transcript).
- **`follow(id, wait=)`** repeats `look` every `POLL_INTERVAL` until
  `_outcome` gives one, or `wait` runs out (`timeout`). Outcomes, in order:
  `rejected`, `expired`, `replied` (reply final), `blocked` (the target's
  registry `waitingFor` is set), `stopped` (not running, not delivered),
  `no_reply` (delivered, idle, something after the delivery, but no text).
  `sleep`, `clock` and `snapshot` are injectable for tests. MCP caps `wait`
  at `MAX_WAIT`.
- Output: `format_follow` (text, with a `Next:` hint naming `log --after
  CURSOR` and, for `timeout`/`blocked`, `follow` again; the MCP tool drops
  it via `_text`) and `follow_json`.
- Instrumented as `follow.done` (outcome, delivery, reply,
  approval=`verifiable/recipient_check`, waited).
