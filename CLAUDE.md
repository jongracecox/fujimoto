# fujimoto

CLI/TUI tool for managing Claude Code sessions in git worktrees and
repositories. This file holds instructions for agents only. Everything a
developer needs to know lives in `docs/`, indexed below.

## Developer docs index

@docs/README.md

## Read before you change

- **Before changing a module, read its `docs/dev/modules/<module>.md`.** Before
  any TUI change, also read `docs/dev/design-decisions.md`. Many decisions
  there exist because the obvious approach was tried and broke.
- **Check `docs/dev/gotchas.md` whenever something behaves strangely**
  (Textual markup, tmux key bindings, Claude log formats, redaction). The
  answer is often already written down.
- Use the Keywords column above to find the doc. If nothing matches, grep
  `docs/`.

## Keeping docs current

Docs are part of every change, not a follow-up:

- Behaviour or architecture of a module → its `docs/dev/modules/*.md`.
- A design choice and the reason for it → `docs/dev/design-decisions.md`.
- Something surprising that cost time → `docs/dev/gotchas.md`.
- A new doc → add a row to the table in `docs/README.md` (path, title,
  keywords). Update the keywords when a doc's content changes.
- User-facing behaviour → `README.md`. Developer setup and code style →
  `CONTRIBUTING.md`.
- **Keep this file for agent instructions only.** It is loaded into every
  session, and Claude Code caps instruction files at 150k characters in
  total. Developer knowledge goes in `docs/`.

## Working rules

- **Instrument every new feature in the same change** (subprocesses, file I/O
  outside the repo, discovery and fallbacks, silent skips). How to do it,
  including redaction, is in `docs/dev/diagnostic-logging.md`.
- **Write tests for all new code**: unit tests for logic, Textual pilot tests
  for UI flows (`docs/dev/testing.md`). Use `# pragma: no cover` only with a
  justification.
- **Don't run `ruff`, `ty` or other linters by hand.** The pre-commit hooks
  run them, so fix whatever they report at commit time. All widget state must
  be typed instance variables, not dynamic attributes on widgets.
- **Keep `src/fujimoto/templates/agent_instructions.md` in step with the CLI.**
  A test parses every `fujimoto …` example in it.
- To try a change in the globally installed `fujimoto`, run
  `uv tool install --force --reinstall --no-cache .` and check that
  `fujimoto --version` changed. Without `--no-cache`, uv silently keeps the
  old build.
- Smoke-test Claude log parsing against real `~/.claude/projects/` data after
  changing it, but never copy that data into tests or docs.
