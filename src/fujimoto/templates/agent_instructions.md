# fujimoto: session tools for agents

Every Claude Code session on this machine leaves a transcript. fujimoto lets
you search those transcripts, read them, and talk to the sessions that wrote
them, whether they are running now or finished long ago. Use it when the
answer to your question is probably in another session's history rather than
in the code: who worked on this, what did they decide, is it really done, is
someone doing it right now.

None of these commands needs the fujimoto TUI. All of them print text by
default and JSON with `--json`, and exit 1 on error (0 for "no matches").

## Commands

### Find sessions: `fujimoto search QUERY`

```sh
fujimoto search ABC-123                        # this project and its worktrees
fujimoto search ABC-123 --all-projects         # every transcript on the machine
fujimoto search ABC-123 --sort relevance -n 5  # the 5 sessions that discuss it most
fujimoto search 'retry(ing)? \d+' -e           # regex (-s: case-sensitive)
fujimoto search "ConnectionResetError" --raw   # also search tool calls and output
```

- Default scope is the repo containing the current directory, plus its
  worktrees (including deleted ones). `--project NAME|PATH` picks another.
- **By default only message text is searched**: what the user and the agent
  said. That is usually what you want. A ticket key turns up in tool output
  for every session that merely ran a ticket search, while a session that
  worked on it keeps naming it in conversation. Add `--raw` for things that
  only exist in tool output (error messages, commands, file contents). Each
  raw hit then shows `N matches: X message, Y tool, Z other`, and
  `0 message` means the session only saw the query, never discussed it.
- `--sort relevance` ranks by message-text matches, so the best candidate is
  first. The default is newest first, which stops scanning at `--limit`
  (default 20) and is faster.
- Each hit gives the session id, project/worktree, branch, title, snippets,
  and whether it is **running**. A running session shows its peer name,
  which is the address to `SendMessage`.
- Your own session is left out (`--include-self` to keep it).

### Read a session: `fujimoto log SESSION`

`SESSION` is a session id, any unique prefix of one, or a `.jsonl` path.

```sh
fujimoto log 8f0e2c1a --summary                # start here
fujimoto log 8f0e2c1a --grep ABC-123 -C 2      # the passages that mention it
fujimoto log 8f0e2c1a --tail 10 --roles user,assistant
fujimoto log 8f0e2c1a --head 5
```

- `--summary` shows the first prompt and the final exchange (the reply the
  user last answered, their answer, the agent's last message). It also lists
  the PR, ticket and Slack links the session mentioned, with ids ready for
  other tools. It is usually enough to know what a session did and how it
  ended.
- `--grep PATTERN` (`-e`, `-s`, `-C N`) jumps to the relevant messages.
  Headings carry each message's index (`#148`), so you can find it again.
- Roles: `user`, `assistant`, `thinking`, `tool_use`, `tool_result`.
  Harness-injected messages (task notifications, reminders) are hidden
  unless you add `--roles ...,injected`.
- If the transcript can't be parsed, the raw lines are printed with a
  warning instead.

### Talk to a session

- **Running:** `SendMessage` to the peer name from `search` or `log`.
- **Finished:** resume it in the background, then message it:

```sh
fujimoto resume 8f0e2c1a --detach --parent <your peer name> \
  --prompt "Question from another session: ... Reply with SendMessage."
fujimoto resume 8f0e2c1a --fork --detach ...   # ask a copy; the original stays as it was
fujimoto stop <tmux session or session id>     # when the conversation is over
```

`resume --detach` prints JSON with `peer_name` (where to `SendMessage`),
`claude_session_id` (a fork's new id) and `stop_command`. A session that is
already running is never started twice; you get its peer name instead. A
session whose directory has been deleted can't be resumed. Read its log
instead. A resumed session has nobody at its keyboard: if it stops at a
permission prompt it waits there, so ask questions it can answer by reading.
Always `stop` what you started.

### Start a session: `fujimoto new TITLE`

```sh
fujimoto new fix-flaky-test --detach --parent <your peer name> \
  --prompt "..." -- --name fix-flaky-test
```

Creates a worktree and starts a session in it. The JSON includes its tmux
session and id, and `-- --name` sets the peer name you will message it at.

## When to use them

Each of these is a starting point. Combine the commands as the question needs.

- **Trace work on a ticket, PR, branch or error.** `search` it (`--sort
  relevance`), then `log --summary` on the top hit, then ask the session if
  the log leaves a question open.
- **Avoid duplicate work.** Before starting a task, `search` for it. A
  running hit means someone is on it now: message them rather than redo it.
- **Recover your own context.** After compaction, or in a fresh session in a
  worktree, `log --summary` the previous session there.
- **Find a past decision.** `search` the option names, then `log --grep X -C 2`
  for the reasoning.
- **Find how an error was fixed before.** `search "<error text>" --raw`, then
  read what the session did after it hit the error.
- **Check that something is really done.** A status says what someone
  intended. The final exchange in `log --summary` says what happened,
  including any hand-off still waiting on someone. Follow its links.
- **Report across sessions.** `--summary --json` on each hit and collect the
  `links`: which PRs and tickets came out of which sessions.
- **Delegate and supervise.** `new --detach --parent ...`, follow progress
  with `log --tail`, steer it with `SendMessage`, then `stop` it.
