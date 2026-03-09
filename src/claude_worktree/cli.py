from __future__ import annotations

import sys
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from claude_worktree.config import (
    ConfigError,
    build_worktree_path,
    get_project_worktrees_dir,
)
from claude_worktree.git import (
    GitError,
    create_worktree,
    get_current_branch,
    get_default_branch,
    get_project_name,
)
from claude_worktree.tmux import (
    TmuxError,
    install_tmux,
    is_tmux_installed,
    launch_claude_in_tmux,
    list_project_sessions,
    session_name,
)

CSS = """\
Screen {
    background: $surface;
}

#main {
    width: 100%;
    height: 100%;
    padding: 1 2;
}

#home-panel {
    height: auto;
}

#home-panel .section-label {
    text-style: bold;
    margin-bottom: 0;
}

#home-list {
    height: auto;
    max-height: 24;
}

#home-list > ListItem {
    padding: 0 2;
}

#home-list:focus > ListItem.--highlight {
    background: $accent;
}

.separator-item {
    color: $text-muted;
    height: 1;
}

#create-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#create-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#create-panel Input {
    margin-bottom: 1;
}

#branch-list {
    height: auto;
    max-height: 6;
    margin-bottom: 1;
}

#branch-list:focus > ListItem.--highlight {
    background: $accent;
}

#conflict-panel {
    height: auto;
    padding: 1 2;
    border: round $warning;
}

#conflict-panel .form-label {
    margin-bottom: 1;
    text-style: bold;
}

#conflict-list {
    height: auto;
    max-height: 6;
}

#conflict-list:focus > ListItem.--highlight {
    background: $accent;
}

.hint {
    color: $text-muted;
    margin-top: 1;
}
"""


class WorktreeApp(App):
    TITLE = "Worktree Manager"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "go_back", "Back", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._project_name: str = ""
        self._current_branch: str = ""
        self._default_branch: str = ""
        self._active_sessions: set[str] = set()
        self._title_value: str = ""
        self._base_branch: str = ""
        self._worktree_path: Path | None = None
        self._launch_target: tuple[str, Path] | None = None
        self._existing_worktrees: list[Path] = []
        self._worktree_paths: dict[str, Path] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="main")
        yield Footer()

    async def on_mount(self) -> None:
        try:
            if not is_tmux_installed():
                await self._show_tmux_install()
                return
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:
            await self._show_error(str(e))

    def _init_git_info(self) -> None:
        self._project_name = get_project_name()
        self._current_branch = get_current_branch()
        self._default_branch = get_default_branch()
        self._active_sessions = set(list_project_sessions(self._project_name))
        self.sub_title = self._project_name

        self._existing_worktrees = []
        try:
            project_dir = get_project_worktrees_dir(self._project_name)
            if project_dir.exists():
                self._existing_worktrees = sorted(
                    [d for d in project_dir.iterdir() if d.is_dir()],
                    key=lambda p: p.name,
                    reverse=True,
                )
        except ConfigError:
            pass

    async def _clear_main(self) -> None:
        main = self.query_one("#main")
        await main.remove_children()

    async def _show_error(self, message: str) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Static(f"[bold red]Error:[/] {message}", markup=True),
        )

    async def _show_tmux_install(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label("tmux is not installed", classes="form-label"),
                Static("tmux is required to manage worktree sessions."),
                Static(""),
                ListView(
                    ListItem(Label("Install with brew"), id="install-tmux"),
                    ListItem(Label("Quit"), id="quit-app"),
                    id="tmux-install-list",
                ),
                id="conflict-panel",
            )
        )
        self.query_one("#tmux-install-list").focus()

    async def _show_home(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        items: list[ListItem] = [
            ListItem(
                Label("[bold]+ Create a new worktree[/]", markup=True),
                id="action-create",
            ),
        ]

        if self._existing_worktrees:
            items.append(
                ListItem(
                    Static("───── existing worktrees ─────", classes="separator-item"),
                    disabled=True,
                ),
            )

            self._worktree_paths = {}
            for wt in self._existing_worktrees:
                sname = session_name(self._project_name, wt.name)
                is_active = sname in self._active_sessions
                if is_active:
                    label_text = f"\U0001f7e2 {wt.name}"
                else:
                    label_text = f"   {wt.name}"
                item_id = f"wt-{wt.name}"
                self._worktree_paths[item_id] = wt
                items.append(ListItem(Label(label_text), id=item_id))

        await main.mount(
            Container(
                ListView(*items, id="home-list"),
                id="home-panel",
            )
        )
        self.query_one("#home-list").focus()

    async def _show_create_form(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label("Create New Worktree", classes="form-label"),
                Static(""),
                Label("Title:"),
                Input(placeholder="e.g. fix-unit-tests", id="title-input"),
                Static("[dim]Press Enter to continue[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        self.query_one("#title-input").focus()

    async def _show_branch_select(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        if self._current_branch == self._default_branch:
            self._base_branch = self._default_branch
            await self._finalize_create()
            return

        await main.mount(
            Container(
                Label("Select Base Branch", classes="form-label"),
                ListView(
                    ListItem(
                        Label(f"Current branch ({self._current_branch})"),
                        id="branch-current",
                    ),
                    ListItem(
                        Label(f"Default branch ({self._default_branch})"),
                        id="branch-default",
                    ),
                    id="branch-list",
                ),
                id="create-panel",
            )
        )
        self.query_one("#branch-list").focus()

    async def _finalize_create(self) -> None:
        self._worktree_path = build_worktree_path(self._project_name, self._title_value)

        if self._worktree_path.exists():
            await self._show_conflict()
            return

        await self._do_create_and_launch()

    async def _show_conflict(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        await main.mount(
            Container(
                Label(
                    f"Worktree already exists: {self._worktree_path.name}",
                    classes="form-label",
                ),
                Static(""),
                ListView(
                    ListItem(
                        Label("Connect to existing worktree"), id="conflict-connect"
                    ),
                    ListItem(
                        Label("Create new with numeric suffix"), id="conflict-suffix"
                    ),
                    id="conflict-list",
                ),
                id="conflict-panel",
            )
        )
        self.query_one("#conflict-list").focus()

    async def _do_create_and_launch(self) -> None:
        new_branch = f"worktree/{self._worktree_path.name}"
        try:
            create_worktree(self._worktree_path, self._base_branch, new_branch)
        except GitError as e:
            await self._show_error(str(e))
            return
        self._launch_target = (self._project_name, self._worktree_path)
        self.exit()

    @on(ListView.Selected, "#home-list")
    async def on_home_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id == "action-create":
            await self._show_create_form()
        elif item_id and item_id in self._worktree_paths:
            self._launch_target = (self._project_name, self._worktree_paths[item_id])
            self.exit()

    @on(ListView.Selected, "#tmux-install-list")
    async def on_tmux_install_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "install-tmux":
            try:
                install_tmux()
                self._init_git_info()
                await self._show_home()
            except (TmuxError, ConfigError, GitError) as e:
                await self._show_error(str(e))
        else:
            self.exit()

    @on(Input.Submitted, "#title-input")
    async def on_title_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        self._title_value = value
        await self._show_branch_select()

    @on(ListView.Selected, "#branch-list")
    async def on_branch_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "branch-current":
            self._base_branch = self._current_branch
        else:
            self._base_branch = self._default_branch
        await self._finalize_create()

    @on(ListView.Selected, "#conflict-list")
    async def on_conflict_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "conflict-connect":
            self._launch_target = (self._project_name, self._worktree_path)
            self.exit()
        elif event.item.id == "conflict-suffix":
            suffix = 2
            while (
                self._worktree_path.parent / f"{self._worktree_path.name}-{suffix}"
            ).exists():
                suffix += 1
            self._worktree_path = (
                self._worktree_path.parent / f"{self._worktree_path.name}-{suffix}"
            )
            await self._do_create_and_launch()

    async def action_go_back(self) -> None:
        if len(self.query("#home-list")) > 0:
            self.exit()
        else:
            try:
                await self._show_home()
            except (ConfigError, GitError):
                self.exit()


def main() -> None:
    try:
        app = WorktreeApp()
        app.run()

        if app._launch_target:
            project_name, worktree_path = app._launch_target
            launch_claude_in_tmux(project_name, worktree_path)
    except (ConfigError, GitError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except TmuxError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
