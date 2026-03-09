from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from textual import events, on
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

from fujimoto.config import (
    ConfigError,
    build_worktree_path,
    get_next_direct_session_name,
    get_project_worktrees_dir,
    get_worktree_root,
    list_projects,
    read_session_meta,
    slugify,
    store_session_meta,
)
from fujimoto.git import (
    GitError,
    cherry_pick_branch,
    create_worktree,
    delete_branch,
    get_current_branch,
    get_default_branch,
    get_project_name,
    get_repo_root,
    get_unpushed_commits,
    has_remote_branch,
    is_branch_merged,
    push_branch,
    remove_worktree,
)
from fujimoto.tmux import (
    TmuxError,
    create_session_with_command,
    install_tmux,
    is_tmux_installed,
    kill_session,
    launch_claude_in_tmux,
    list_project_sessions,
    rename_session,
    session_name,
)

BRANCH_ICON = "\ue0a0"

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

#actions-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#actions-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#actions-panel .session-info {
    color: $text-muted;
    margin-bottom: 1;
}

#session-actions {
    height: auto;
    max-height: 8;
}

#session-actions:focus > ListItem.--highlight {
    background: $accent;
}

#finish-panel {
    height: auto;
    padding: 1 2;
    border: round $warning;
}

#finish-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#finish-panel .branch-status {
    color: $text-muted;
    margin-bottom: 1;
}

#finish-list {
    height: auto;
    max-height: 8;
}

#finish-list:focus > ListItem.--highlight {
    background: $accent;
}

#confirm-panel {
    height: auto;
    padding: 1 2;
    border: round $error;
}

#confirm-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#confirm-panel .warning-text {
    color: $warning;
    margin-bottom: 1;
}

#confirm-list {
    height: auto;
    max-height: 4;
}

#confirm-list:focus > ListItem.--highlight {
    background: $accent;
}

#project-panel {
    height: auto;
    padding: 1 2;
    border: round $primary;
}

#project-panel .form-label {
    margin-bottom: 0;
    text-style: bold;
}

#project-filter {
    margin-bottom: 1;
}

#project-list {
    height: auto;
    max-height: 20;
}

#project-list > ListItem {
    padding: 0 2;
}

#project-list:focus > ListItem.--highlight {
    background: $accent;
}
"""


@dataclass
class SessionInfo:
    name: str
    session_type: str  # "worktree" or "direct"
    project: str
    path: Path
    tmux_session: str
    is_active: bool
    branch: str


class SessionApp(App):
    TITLE = "Session Manager"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("escape", "go_back", "Back", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._project_cwd: Path | None = None
        self._project_name: str = ""
        self._current_branch: str = ""
        self._default_branch: str = ""
        self._active_sessions: set[str] = set()
        self._title_value: str = ""
        self._base_branch: str = ""
        self._worktree_path: Path | None = None
        self._launch_target: tuple[str, Path, str | None] | None = None
        self._existing_worktrees: list[Path] = []
        self._session_map: dict[str, SessionInfo] = {}
        self._available_projects: list[Path] = []
        self._project_dir_paths: dict[str, Path] = {}
        self._selected_session: SessionInfo | None = None
        self._finish_action: str = ""

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
        cwd = self._project_cwd
        self._project_name = get_project_name(cwd)
        self._current_branch = get_current_branch(cwd)
        self._default_branch = get_default_branch(cwd)
        self._active_sessions = set(list_project_sessions(self._project_name))
        self._available_projects = list_projects()
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
                Static("tmux is required to manage sessions."),
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

    # -- Home screen --

    async def _show_home(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        items: list[ListItem] = [
            ListItem(
                Label("[bold]+ New worktree session[/]", markup=True),
                id="action-create",
            ),
            ListItem(
                Label(
                    f"[bold]+ New session in {self._project_name}[/]",
                    markup=True,
                ),
                id="action-direct",
            ),
        ]

        # Build session map for all items
        self._session_map = {}

        # Collect direct sessions (active tmux sessions without matching worktrees)
        worktree_session_names = set()
        for wt in self._existing_worktrees:
            sname = session_name(self._project_name, wt.name)
            worktree_session_names.add(sname)

        direct_sessions: list[str] = []
        for sname in sorted(self._active_sessions):
            if sname not in worktree_session_names:
                direct_sessions.append(sname)

        # Active sessions section
        active_worktrees = [
            wt
            for wt in self._existing_worktrees
            if session_name(self._project_name, wt.name) in self._active_sessions
        ]
        has_active = bool(active_worktrees) or bool(direct_sessions)

        if has_active:
            items.append(
                ListItem(
                    Static(
                        "───── active sessions ─────",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )

            for sname in direct_sessions:
                item_id = f"ds-{sname.replace('/', '--')}"
                # Direct sessions: extract the suffix part for display
                display_name = sname.split("/", 1)[1] if "/" in sname else sname
                self._session_map[item_id] = SessionInfo(
                    name=display_name,
                    session_type="direct",
                    project=self._project_name,
                    path=self._project_cwd or Path("."),
                    tmux_session=sname,
                    is_active=True,
                    branch=self._current_branch,
                )
                label_text = (
                    f"\U0001f7e2 {display_name}"
                    f"  [dim]({self._project_name} {BRANCH_ICON} {self._current_branch})[/]"
                )
                items.append(ListItem(Label(label_text, markup=True), id=item_id))

            for wt in active_worktrees:
                sname = session_name(self._project_name, wt.name)
                item_id = f"wt-{wt.name}"
                branch = f"worktree/{wt.name}"
                self._session_map[item_id] = SessionInfo(
                    name=wt.name,
                    session_type="worktree",
                    project=self._project_name,
                    path=wt,
                    tmux_session=sname,
                    is_active=True,
                    branch=branch,
                )
                label_text = f"\U0001f7e2 {wt.name}  [dim]({BRANCH_ICON} {branch})[/]"
                items.append(ListItem(Label(label_text, markup=True), id=item_id))

        # Inactive worktrees section
        inactive_worktrees = [
            wt
            for wt in self._existing_worktrees
            if session_name(self._project_name, wt.name) not in self._active_sessions
        ]

        if inactive_worktrees:
            items.append(
                ListItem(
                    Static(
                        "───── inactive worktrees ─────",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            for wt in inactive_worktrees:
                sname = session_name(self._project_name, wt.name)
                item_id = f"wt-{wt.name}"
                branch = f"worktree/{wt.name}"
                self._session_map[item_id] = SessionInfo(
                    name=wt.name,
                    session_type="worktree",
                    project=self._project_name,
                    path=wt,
                    tmux_session=sname,
                    is_active=False,
                    branch=branch,
                )
                label_text = f"\u26ab {wt.name}  [dim]({BRANCH_ICON} {branch})[/]"
                items.append(ListItem(Label(label_text, markup=True), id=item_id))

        if self._available_projects:
            items.append(
                ListItem(
                    Static(
                        "\u2500\u2500\u2500\u2500\u2500"
                        "\u2500\u2500\u2500\u2500\u2500"
                        "\u2500\u2500\u2500\u2500\u2500"
                        "\u2500\u2500\u2500\u2500\u2500"
                        "\u2500\u2500\u2500\u2500\u2500"
                        "\u2500\u2500\u2500\u2500",
                        classes="separator-item",
                    ),
                    disabled=True,
                ),
            )
            items.append(
                ListItem(
                    Label(
                        f"[dim]Switch project (current: {self._project_name})[/]",
                        markup=True,
                    ),
                    id="action-switch-project",
                ),
            )

        await main.mount(
            Container(
                ListView(*items, id="home-list"),
                id="home-panel",
            )
        )
        self.query_one("#home-list").focus()

    # -- Session actions submenu --

    async def _show_session_actions(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        items: list[ListItem] = []

        if session.is_active:
            items.append(ListItem(Label("Connect"), id="sa-connect"))
            items.append(ListItem(Label("Terminate session"), id="sa-terminate"))
        else:
            items.append(ListItem(Label("Launch"), id="sa-launch"))

        items.append(ListItem(Label("Rename"), id="sa-rename"))

        if session.session_type == "worktree":
            items.append(ListItem(Label("Finish (cleanup/merge)"), id="sa-finish"))

        items.append(ListItem(Label("[dim]Cancel[/]", markup=True), id="sa-cancel"))

        type_label = session.project if session.session_type == "direct" else "worktree"
        status_label = "active" if session.is_active else "inactive"
        info_text = f"{type_label} | {status_label} | {BRANCH_ICON} {session.branch}"

        await main.mount(
            Container(
                Label(session.name, classes="form-label"),
                Static(info_text, classes="session-info"),
                ListView(*items, id="session-actions"),
                id="actions-panel",
            )
        )
        self.query_one("#session-actions").focus()

    # -- Rename flow --

    async def _show_rename(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        current_suffix = (
            session.tmux_session.split("/", 1)[1]
            if "/" in session.tmux_session
            else session.tmux_session
        )

        await main.mount(
            Container(
                Label(f"Rename: {session.name}", classes="form-label"),
                Static(""),
                Label("New name:"),
                Input(value=current_suffix, id="rename-input"),
                Static("[dim]Press Enter to rename[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        rename_input = self.query_one("#rename-input", Input)
        rename_input.focus()
        rename_input.cursor_position = len(rename_input.value)

    # -- Finish flow --

    async def _show_finish(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        branch = session.branch
        meta = read_session_meta(session.path)
        base = meta.get("base_branch", self._default_branch)

        # Check branch status
        try:
            unpushed = get_unpushed_commits(branch, cwd=self._project_cwd)
            merged = is_branch_merged(branch, base, cwd=self._project_cwd)
            has_remote = has_remote_branch(branch, cwd=self._project_cwd)
        except GitError:
            unpushed = []
            merged = False
            has_remote = False

        items: list[ListItem] = []

        if merged:
            status_text = f"Branch {branch} has been merged into {base}."
            items.append(ListItem(Label("Delete worktree"), id="finish-delete"))
            if has_remote:
                items.append(
                    ListItem(
                        Label("Delete worktree + remote branch"),
                        id="finish-delete-remote",
                    )
                )
        else:
            commit_count = len(unpushed)
            if commit_count > 0 and not has_remote:
                status_text = (
                    f"Branch {branch} has {commit_count} commit(s) "
                    f"not pushed to any remote."
                )
            elif commit_count > 0:
                status_text = f"Branch {branch} has {commit_count} unpushed commit(s)."
            else:
                status_text = f"Branch {branch} is up to date with origin."

            items.append(ListItem(Label("Push & Create PR"), id="finish-pr"))
            items.append(
                ListItem(
                    Label(f"Cherry-pick to {base}"),
                    id="finish-cherry-pick",
                )
            )
            items.append(ListItem(Label("Discard & Delete"), id="finish-discard"))

        items.append(ListItem(Label("[dim]Cancel[/]", markup=True), id="finish-cancel"))

        await main.mount(
            Container(
                Label(f"Finish: {session.name}", classes="form-label"),
                Static(status_text, classes="branch-status"),
                ListView(*items, id="finish-list"),
                id="finish-panel",
            )
        )
        self.query_one("#finish-list").focus()

    # -- Confirmation dialog --

    async def _show_confirm_discard(self, session: SessionInfo) -> None:
        self._selected_session = session
        await self._clear_main()
        main = self.query_one("#main")

        branch = session.branch
        try:
            unpushed = get_unpushed_commits(branch, cwd=self._project_cwd)
        except GitError:
            unpushed = []

        if unpushed:
            warning = (
                f"{len(unpushed)} commit(s) will be lost.\n"
                "The branch will be deleted and cannot be recovered."
            )
        else:
            warning = "The worktree directory and branch will be removed."

        await main.mount(
            Container(
                Label(
                    f"Delete worktree {session.name}?",
                    classes="form-label",
                ),
                Static(warning, classes="warning-text"),
                ListView(
                    ListItem(Label("Delete"), id="confirm-yes"),
                    ListItem(Label("Cancel"), id="confirm-no"),
                    id="confirm-list",
                ),
                id="confirm-panel",
            )
        )
        self.query_one("#confirm-list").focus()

    # -- Create worktree flow --

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
        try:
            self._worktree_path = build_worktree_path(
                self._project_name, self._title_value
            )
        except ConfigError as e:
            await self._show_error(str(e))
            return

        if self._worktree_path.exists():
            await self._show_conflict()
            return

        await self._do_create_and_launch()

    async def _show_conflict(self) -> None:
        assert self._worktree_path is not None
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
        assert self._worktree_path is not None
        new_branch = f"worktree/{self._worktree_path.name}"
        try:
            create_worktree(
                self._worktree_path,
                self._base_branch,
                new_branch,
                cwd=self._project_cwd,
            )
            store_session_meta(self._worktree_path, self._base_branch)
        except GitError as e:
            await self._show_error(str(e))
            return
        self._launch_target = (self._project_name, self._worktree_path, None)
        self.exit()

    # -- Project switcher --

    def _build_project_items(self, filter_text: str = "") -> list[ListItem]:
        self._project_dir_paths = {}
        items: list[ListItem] = []
        query = filter_text.lower()
        for proj in self._available_projects:
            if query and query not in proj.name.lower():
                continue
            item_id = f"proj-{proj.name}"
            self._project_dir_paths[item_id] = proj
            if proj.name == self._project_name:
                label_text = f"\U0001f7e2 {proj.name}"
            else:
                label_text = f"   {proj.name}"
            items.append(ListItem(Label(label_text), id=item_id))
        return items

    async def _show_project_select(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")

        items = self._build_project_items()

        await main.mount(
            Container(
                Label("Switch Project", classes="form-label"),
                Input(placeholder="Type to filter...", id="project-filter"),
                ListView(*items, id="project-list"),
                id="project-panel",
            )
        )
        self.query_one("#project-filter").focus()

    def _update_project_suggestion(self) -> None:
        """Set ghost text on the filter input based on the highlighted list item."""
        filter_input = self.query_one("#project-filter", Input)
        project_list = self.query_one("#project-list", ListView)
        typed = filter_input.value
        if len(project_list) > 0 and project_list.index is not None:
            item = project_list.children[project_list.index]
            if item.id and item.id in self._project_dir_paths:
                name = self._project_dir_paths[item.id].name
                if name.lower().startswith(typed.lower()):
                    filter_input._suggestion = typed + name[len(typed) :]
                    return
        filter_input._suggestion = ""

    # -- Event handlers --

    @on(Input.Changed, "#project-filter")
    async def on_project_filter_changed(self, event: Input.Changed) -> None:
        project_list = self.query_one("#project-list", ListView)
        await project_list.clear()
        for item in self._build_project_items(event.value):
            await project_list.append(item)
        if len(project_list) > 0:
            project_list.index = 0
        self._update_project_suggestion()

    @on(Input.Submitted, "#project-filter")
    async def on_project_filter_submitted(self, event: Input.Submitted) -> None:
        await self._select_highlighted_project()

    async def _select_highlighted_project(self) -> None:
        """Select whichever project is currently highlighted in the list."""
        project_list = self.query_one("#project-list", ListView)
        if len(project_list) == 0 or project_list.index is None:
            return
        item = project_list.children[project_list.index]
        item_id = item.id
        if item_id and item_id in self._project_dir_paths:
            self._project_cwd = self._project_dir_paths[item_id]
            try:
                self._init_git_info()
                await self._show_home()
            except (ConfigError, GitError) as e:
                await self._show_error(str(e))

    @on(ListView.Selected, "#project-list")
    async def on_project_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id and item_id in self._project_dir_paths:
            self._project_cwd = self._project_dir_paths[item_id]
            try:
                self._init_git_info()
                await self._show_home()
            except (ConfigError, GitError) as e:
                await self._show_error(str(e))

    async def _on_key(self, event: events.Key) -> None:
        """Handle arrow keys and tab for project filter autocomplete."""
        if not (self.focused and self.focused.id == "project-filter"):
            return

        project_list_nodes = self.query("#project-list")
        if not project_list_nodes:
            return
        project_list = self.query_one("#project-list", ListView)

        if event.key in ("down", "up"):
            event.prevent_default()
            event.stop()
            if len(project_list) == 0:
                return
            idx = project_list.index or 0
            if event.key == "down":
                idx = min(idx + 1, len(project_list) - 1)
            else:
                idx = max(idx - 1, 0)
            project_list.index = idx
            self._update_project_suggestion()

        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            filter_input = self.query_one("#project-filter", Input)
            if filter_input._suggestion:
                filter_input.value = filter_input._suggestion
                filter_input.cursor_position = len(filter_input.value)
                filter_input._suggestion = ""

    @on(ListView.Selected, "#home-list")
    async def on_home_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id == "action-create":
            await self._show_create_form()
        elif item_id == "action-direct":
            await self._launch_direct_session()
        elif item_id == "action-switch-project":
            await self._show_project_select()
        elif item_id and item_id in self._session_map:
            await self._show_session_actions(self._session_map[item_id])

    async def _launch_direct_session(self) -> None:
        await self._show_direct_title_form()

    async def _show_direct_title_form(self) -> None:
        await self._clear_main()
        main = self.query_one("#main")
        default_name = get_next_direct_session_name(
            self._project_name, self._active_sessions
        ).split("/", 1)[1]
        await main.mount(
            Container(
                Label(
                    f"New Session in {self._project_name}",
                    classes="form-label",
                ),
                Static(""),
                Label("Session name:"),
                Input(value=default_name, id="direct-title-input"),
                Static("[dim]Press Enter to launch[/]", markup=True, classes="hint"),
                id="create-panel",
            )
        )
        title_input = self.query_one("#direct-title-input", Input)
        title_input.focus()
        title_input.cursor_position = len(title_input.value)

    @on(ListView.Selected, "#session-actions")
    async def on_session_action_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        action = event.item.id

        if action == "sa-connect":
            self._launch_target = (
                session.project,
                session.path,
                session.tmux_session,
            )
            self.exit()
        elif action == "sa-launch":
            self._launch_target = (
                session.project,
                session.path,
                session.tmux_session,
            )
            self.exit()
        elif action == "sa-terminate":
            try:
                kill_session(session.tmux_session)
                self._active_sessions.discard(session.tmux_session)
                self._init_git_info()
                await self._show_home()
            except (TmuxError, ConfigError, GitError) as e:
                await self._show_error(str(e))
        elif action == "sa-rename":
            await self._show_rename(session)
        elif action == "sa-finish":
            await self._show_finish(session)
        elif action == "sa-cancel":
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    @on(ListView.Selected, "#finish-list")
    async def on_finish_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover
        action = event.item.id

        if action == "finish-pr":
            await self._do_push_and_pr(session)
        elif action == "finish-cherry-pick":
            await self._do_cherry_pick(session)
        elif action == "finish-discard":
            self._finish_action = "discard"
            await self._show_confirm_discard(session)
        elif action == "finish-delete":
            self._finish_action = "delete"
            await self._do_delete_worktree(session, remove_remote=False)
        elif action == "finish-delete-remote":
            self._finish_action = "delete-remote"
            await self._do_delete_worktree(session, remove_remote=True)
        elif action == "finish-cancel":
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    @on(ListView.Selected, "#confirm-list")
    async def on_confirm_selected(self, event: ListView.Selected) -> None:
        session = self._selected_session
        if session is None:
            return  # pragma: no cover

        if event.item.id == "confirm-yes":
            await self._do_delete_worktree(session, remove_remote=False)
        else:
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))

    # -- Finish operations --

    async def _do_push_and_pr(self, session: SessionInfo) -> None:
        branch = session.branch
        try:
            push_branch(branch, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Push failed: {e}")
            return

        # Spin up a background Claude session to create the PR
        pr_session_name = f"{session.project}/pr-{session.name}"
        allowed = "Bash(git:*) Bash(gh:*)"
        command = (
            f'claude -p --allowedTools "{allowed}" '
            f'"Push this branch and create a PR. '
            f'Follow project conventions from CLAUDE.md."'
        )
        try:
            create_session_with_command(pr_session_name, session.path, command)
        except Exception as e:  # pragma: no cover
            await self._show_error(f"Failed to start PR session: {e}")
            return

        try:
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

    async def _do_cherry_pick(self, session: SessionInfo) -> None:
        branch = session.branch
        meta = read_session_meta(session.path)
        base = meta.get("base_branch", self._default_branch)

        try:
            cherry_pick_branch(branch, base, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Cherry-pick failed: {e}")
            return

        await self._do_delete_worktree(session, remove_remote=False)

    async def _do_delete_worktree(
        self, session: SessionInfo, remove_remote: bool
    ) -> None:
        # Terminate session if active
        if session.is_active:
            try:
                kill_session(session.tmux_session)
            except TmuxError:
                pass

        # Remove git worktree
        try:
            remove_worktree(session.path, cwd=self._project_cwd)
        except GitError as e:
            await self._show_error(f"Worktree removal failed: {e}")
            return

        # Delete the branch
        try:
            delete_branch(session.branch, remote=remove_remote, cwd=self._project_cwd)
        except GitError:
            pass  # Branch may already be gone

        try:
            self._init_git_info()
            await self._show_home()
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

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

    @on(Input.Submitted, "#direct-title-input")
    async def on_direct_title_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        tmux_name = f"{self._project_name}/{slugify(value)}"
        project_path = self._project_cwd or Path(".")
        self._launch_target = (self._project_name, project_path, tmux_name)
        self.exit()

    @on(Input.Submitted, "#rename-input")
    async def on_rename_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        session = self._selected_session
        if not value or session is None:
            return
        new_tmux_name = f"{session.project}/{slugify(value)}"
        if new_tmux_name == session.tmux_session:
            try:
                await self._show_home()
            except (ConfigError, GitError) as e:  # pragma: no cover
                await self._show_error(str(e))
            return
        try:
            rename_session(session.tmux_session, new_tmux_name)
            self._init_git_info()
            await self._show_home()
        except TmuxError as e:
            await self._show_error(str(e))
        except (ConfigError, GitError) as e:  # pragma: no cover
            await self._show_error(str(e))

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
        assert self._worktree_path is not None
        if event.item.id == "conflict-connect":
            self._launch_target = (self._project_name, self._worktree_path, None)
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


def _check_prerequisites() -> list[str]:
    """Validate environment before launching the TUI. Returns a list of issues."""
    issues: list[str] = []

    try:
        get_worktree_root()
    except ConfigError as e:
        issues.append(str(e))

    try:
        get_repo_root()
    except GitError:
        issues.append(
            "Not inside a git repository.\n"
            "Run fujimoto from within a git project directory."
        )

    return issues


def main() -> None:
    try:
        issues = _check_prerequisites()
        if issues:
            print("fujimoto: configuration error\n", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}\n", file=sys.stderr)
            sys.exit(1)

        while True:
            app = SessionApp()
            app.run()

            if app._launch_target:
                project_name, working_dir, tmux_name = app._launch_target
                launch_claude_in_tmux(project_name, working_dir, tmux_name)
            else:
                break
    except (ConfigError, GitError) as e:
        print(f"\nfujimoto: {e}", file=sys.stderr)
        sys.exit(1)
    except TmuxError as e:
        print(f"\nfujimoto: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
