# `broadcast.py`

User-facing docs: README, "Broadcasting to every session". Pitfalls found
while building it: [gotchas](../gotchas.md#broadcast).

**`broadcast.py`** — `fujimoto broadcast`: one signed message to every
listening session (or one project's), delivered by documented Claude Code
hooks. Imported by the per-session waiter, so it keeps to stdlib + `debug`
at module scope (`global_config`, `claude.live`, `git`, `project_config`
lazily).
- **Config** (`load_config` / `save_config`): the `broadcast` section of the
  global config (`BroadcastConfig` in `global_config.py`) as
  `BroadcastSettings(enabled, signers: list[Signer], signing_key)`. Fails
  closed: an invalid config file reads as disabled (`strict=True` raises
  instead, for the CLI), and a signer whose name or key does not validate is
  dropped (`broadcast.config_signer_skipped`), never trusted.
- **Signer roles** (`Signer.role`, `SignerConfig.role` in the global
  config): `user` for the user's own keys, `agent` (the default for
  `add-signer`) for an agent acting for them; `set-role` changes it. Roles
  travel in the hook token as a third element; a two-element token (a session
  launched before roles) decodes as `user`, which is how those sessions
  worded everything anyway. `preamble()` picks the wording from the role:
  user → "{Broadcast|Message} from the user (via fujimoto, signed by NAME, id
  ID)"; agent → "… from NAME, an agent with a trusted key (via fujimoto, id
  ID)"; agent with a payload `approval` → "… relaying the user's approval:
  “TEXT”. Its key is verified; that approval is its claim, and your own
  permission prompts still apply." The signature proves who relayed it, never
  that the user approved, which is why the approval is quoted rather than
  promoted to "from the user". The id is in every preamble so
  `fujimoto log --reply-to ID` can find the exact delivery.
- **Payload** (`Payload`): `v`, `id`, `project` (`None` = all), `created`,
  `expires`, `message`, optional `approval` (an agent's quote of what the user
  approved, at most `MAX_APPROVAL_CHARS`; omitted when unset), and optional `session` (a full Claude session id,
  omitted from the bytes when unset so pre-existing payloads still verify;
  a targeted payload is `v: 2`, because a waiter that predates `session` ignores
  the field and, with `project: null`, would deliver it to every session —
  `v: 2` is what it rejects instead; `from_bytes` requires `session` iff `v: 2`),
  serialized with sorted keys. `check_time` rejects
  expired, future-dated (beyond `CLOCK_SKEW`) and over-`MAX_TTL` (24h)
  payloads; `covers(project, session_id)` is the scope test: a `session` must equal the
  waiter's own session id, and then the project scope must also cover it.
- **Single-session target** (`send(session=REF)`, CLI `fujimoto send SESSION TEXT`
  via `_run_send_command` — deliberately its own top-level command rather than
  a `broadcast send` flag, since one session isn't a broadcast):
  `resolve_target` matches REF against the *current* listeners — tmux name or
  peer name exactly, else a 4+ character session-id prefix — and must find
  exactly one; the payload then carries that listener's full id and no
  project. Resolving on the sending side and signing the exact id (rather
  than signing the name) is deliberate: a tmux or peer name can be reused by
  a different session before the message expires. `list_listeners` fills
  each `Listener`'s `tmux_session`/`peer_name` from `claude.live` by session
  id (`_registry_names`). A session is not a listener while it is handling a
  broadcast (its waiter has exited and the `Stop` hook has not re-armed yet),
  so `fujimoto send` can briefly fail with "no listening session". A
  targeted payload's `kind` is `Message` (else `Broadcast`), which is the first
  word of the preamble the session sees and of `send`'s output.
- **Signing**: `ssh-keygen -Y sign`/`-Y find-principals`/`-Y verify` in the
  `fujimoto-broadcast` namespace, with an `allowed_signers` file written to a
  temp dir per call and every line restricted to that namespace.
  `resolve_signing_key` takes a key path (public → agent signs, private →
  file signs) or a literal public key, or with neither picks the first
  `ssh-add -L` key that is a configured signer. `verify` returns the signer
  *name* from `find-principals`, never from the payload. `send` verifies its
  own signature before posting, so a key that isn't a signer fails at the
  sender instead of in every session. `submit` posts a payload signed
  elsewhere (`send --signed FILE`, signature at `FILE.sig`).
- **Mailbox** `~/.cache/fujimoto/broadcast/` (`messages/`, `receipts/`,
  `state/`, all 0700): entries are `<arrival-ns>-<payload id>.json` + `.sig`,
  written signature-first via temp + `os.replace` so a waiter never sees half
  a pair. Reads go through `_read_private_file` (O_NOFOLLOW, owner, size).
  `prune` drops entries and receipts `PRUNE_AFTER` (7 days) past expiry —
  the mailbox doubles as the message log behind the TUI's `b` view. Receipts
  carry the verified `signer` (optional, so older receipts still load), which
  is how `StatusRow.signer` names the sender without trusting the payload.
  For rows whose receipts carry no signer (written by a waiter armed before
  the field existed — the waiter keeps the code it started with until its
  next delivery), `status()` re-verifies the stored signature against the
  current signers (`_reverify`, logged as `broadcast.reverify`) and fills
  `StatusRow.verified_signer`.
- **Waiter** (`python -m fujimoto.broadcast wait --signers TOKEN [--project
  P] [--debug|--debug-redacted]`, `main` → `wait`): the hook command, built by
  `hook_command` with `sys.executable`. The signers are base64 JSON *in the
  command line* (`encode_signers`), so a running session's trust can't be
  changed by editing a file. It takes an exclusive lock
  `state/<claude pid>.lock` (`_take_lock`; a dead holder's lock is taken
  over), keyed by `find_claude_pid` — the nearest ancestor with a Claude
  registry file — so `/clear` (new session id, same process) cannot give a
  process two waiters. State `state/<key>.json` holds the high-water `mark`
  (mailbox entry name) and the delivered payload ids; a `SessionStart` with
  `source: startup` resets the mark to "now", so new sessions don't replay old
  broadcasts. Each poll (`POLL_INTERVAL` 1s) runs `check_entry` on entries past
  the mark: out-of-scope is skipped silently, a delivery or rejection writes a
  `Receipt` (`receipts/<entry>/<session id>.json`). Delivery exits
  `EXIT_DELIVER` (2) with `render()`'s text on stderr. Lock files double as
  the listener list (`list_listeners`), which `send` uses to know how many
  receipts to wait for.
- **Launch** (`launch_args`): adds `--settings` with `hook_settings` (the
  waiter as `SessionStart` and `Stop` hooks, `async` + `asyncRewake`,
  `timeout=HOOK_TIMEOUT` 7 days) when the global config's `broadcast.enabled`, there is
  at least one signer, and the project's `.fujimoto.yaml` doesn't say
  `broadcast: false`. `merge_settings_args` merges into an existing
  `--settings` (inline JSON, `--settings=`, or a file relative to the
  working dir) — see `docs/dev/gotchas.md` (Broadcast). It never raises: a failure is logged
  (`broadcast.launch included=False reason=…`) and printed, and the session
  launches without broadcast.
- Output: `format_send`/`send_json`, `format_status`/`status_json`,
  `format_show`/`show_json`.
- Instrumented as `broadcast.ssh_keygen` (op, rc), `broadcast.signing_key`
  (`via=path|literal|agent`), `broadcast.posted`, `broadcast.sent`,
  `broadcast.pruned`, `broadcast.launch`, `broadcast.wait` (`armed`,
  `already-armed`, `error`) and `broadcast.wait_entry` (outcome + reason). The
  waiter only logs when the launching fujimoto ran with `--debug`, which
  `launch_args` passes through to the hook command.
