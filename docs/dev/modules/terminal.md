# `terminal.py`

**`terminal.py`** — Open native terminal windows in a session's directory:
- `open_terminal(directory)` — platform dispatch. macOS: iTerm2 (if installed) → Terminal.app. Linux: `FUJIMOTO_TERMINAL` env var → auto-detected emulator. Raises `OSError` on unsupported platforms or when no terminal is found.
- `_has_iterm()` — checks for `/Applications/iTerm.app`
- `_open_iterm(directory)` — AppleScript to create new iTerm2 window
- `_open_terminal_app(directory)` — `open -a Terminal` fallback
- `_open_linux_terminal(directory)` — uses `FUJIMOTO_TERMINAL` if set; otherwise probes `_LINUX_TERMINALS` (gnome-terminal, konsole, kitty, alacritty, wezterm, foot, xfce4-terminal, tilix, terminator, xterm) and spawns the first one found via `subprocess.Popen` with `start_new_session=True` so it survives parent exit.
- `_format_args(args, directory)` — substitutes `{dir}` placeholder; appends directory if no placeholder present.
