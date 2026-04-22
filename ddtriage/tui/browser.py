"""Textual-based interactive file tree browser for ddtriage."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import (
    Footer, Header, Static, Tree as TextualTree, Input, Label,
)
from textual.widgets.tree import TreeNode

from ..health import FileHealth, HealthStatus, compute_tree_health
from ..mapfile.parser import Mapfile
from ..ntfs.tree import DirectoryTree, FileRecord, ROOT_MFT_INDEX


class SelectableTree(TextualTree):
    """Tree widget that uses space for selection instead of expand/collapse.

    We override action_toggle_node (bound to space by the parent Tree class)
    to post a custom message that the app handles for selection. Enter still
    expands/collapses via action_select_cursor. Mouse clicks also toggle
    selection (the expand arrow still works for expand/collapse).
    """

    class SelectionRequested(Message):
        """Posted when space is pressed or a node is clicked."""
        pass

    def action_toggle_node(self) -> None:
        """Override space: request selection instead of expand/collapse."""
        self.post_message(self.SelectionRequested())

    BINDINGS = [
        Binding("right", "expand_node", "Expand", show=False),
        Binding("left", "collapse_node", "Collapse", show=False),
    ]

    def action_select_cursor(self) -> None:
        """Enter: expand/collapse the current node."""
        node = self.cursor_node
        if node is not None:
            node.toggle()

    def action_expand_node(self) -> None:
        """Right arrow: expand folder, or move to first child if already expanded."""
        node = self.cursor_node
        if node is None:
            return
        if not node.is_expanded and node.children:
            node.expand()
        elif node.children:
            # Already expanded — move cursor to first child
            self.action_cursor_down()

    def action_collapse_node(self) -> None:
        """Left arrow: collapse folder, or move to parent if already collapsed."""
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None:
            self.select_node(node.parent)
            node.parent.collapse()

    async def _on_click(self, event) -> None:
        """Mouse click on a node expands/collapses it (same as Enter).

        Selection is toggled via space key only — Textual's Tree widget
        doesn't support detecting click position within labels, so we
        can't distinguish clicking the indicator vs the name.
        """
        meta = event.style.meta
        if "line" not in meta:
            return
        cursor_line = meta["line"]
        self.cursor_line = cursor_line
        node = self.get_node_at_line(cursor_line)
        if node is not None:
            node.toggle()
from ..selection import (
    export_selection, import_selection, collect_selection_with_children,
)


def _human_size(nbytes: int) -> str:
    if nbytes < 0:
        return "0 B"
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != 'B' else f"{nbytes} B"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} PB"


# ---------------------------------------------------------------------------
# Custom tree widget with checkboxes
# ---------------------------------------------------------------------------

class FileTreeData:
    """Data attached to each tree node."""
    def __init__(self, record: FileRecord, health: FileHealth | None = None):
        self.record = record
        self.health = health
        self.selected = False


class RecoveryBrowser(App):
    """Main TUI application for browsing and selecting files."""

    TITLE = "ddtriage"
    CSS = """
    #main-container {
        layout: horizontal;
    }
    #tree-panel {
        width: 2fr;
        height: 100%;
        border: solid $primary;
    }
    #info-panel {
        width: 1fr;
        height: 100%;
        border: solid $secondary;
        padding: 1;
    }
    #search-bar {
        dock: top;
        display: none;
        height: 3;
        padding: 0 1;
    }
    #search-bar.visible {
        display: block;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    .health-complete { color: green; }
    .health-partial { color: yellow; }
    .health-unread { color: red; }
    .health-unknown { color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_select", "Select", show=True, key_display="space", priority=True),
        Binding("r", "start_recovery", "Recover", show=True),
        Binding("s", "save_selection", "Save sel.", show=True),
        Binding("l", "load_selection", "Load sel.", show=True),
        Binding("slash", "search", "Search", show=True),
        Binding("escape", "cancel_search", "Cancel", show=False),
        Binding("a", "select_all", "Select all", show=True),
        Binding("u", "unselect_all", "Unselect all", show=True),
    ]

    selected_count: reactive[int] = reactive(0)
    selected_size: reactive[int] = reactive(0)
    bytes_to_read: reactive[int] = reactive(0)

    def __init__(
        self,
        dir_tree: DirectoryTree,
        mapfile: Mapfile | None = None,
        cluster_size: int = 4096,
        partition_offset: int = 0,
        device_name: str = "",
        image_path: str = "",
        work_dir: str = ".",
        mft_coverage_pct: float = 100.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.dir_tree = dir_tree
        self.mapfile = mapfile
        self.cluster_size = cluster_size
        self.partition_offset = partition_offset
        self.device_name = device_name
        self.image_path = image_path
        self.work_dir = work_dir
        self.mft_coverage_pct = mft_coverage_pct

        # Compute health for all records
        self.health_map: dict[int, FileHealth] = {}
        if mapfile:
            self.health_map = compute_tree_health(
                dir_tree.all_records, mapfile, cluster_size, partition_offset,
            )

        self.selected_indices: set[int] = set()
        # Maps mft_index → TreeNode for fast lookup
        self._node_map: dict[int, TreeNode[FileTreeData]] = {}

        self._recovery_requested = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="search-bar"):
            yield Input(placeholder="Search filename...", id="search-input")
        with Horizontal(id="main-container"):
            yield SelectableTree("", id="file-tree")
            yield Vertical(
                Static("", id="info-content"),
                id="info-panel",
            )
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.device_name} → {self.image_path}"
        tree_widget = self.query_one("#file-tree", SelectableTree)
        tree_widget.root.set_label("/")

        # Add the root record to the node map but populate its children
        # directly under the tree widget root (so they're visible immediately)
        root_rec = self.dir_tree.root
        root_health = self.health_map.get(root_rec.mft_index)
        root_data = FileTreeData(root_rec, root_health)
        tree_widget.root.data = root_data
        self._node_map[root_rec.mft_index] = tree_widget.root

        for child in sorted(root_rec.children, key=lambda c: (not c.is_directory, c.name.lower())):
            self._populate_node(tree_widget.root, child)

        tree_widget.root.expand()
        self._update_status_bar()

    def _populate_node(
        self, parent_node: TreeNode, record: FileRecord,
    ) -> None:
        """Recursively populate the tree widget from a FileRecord."""
        health = self.health_map.get(record.mft_index)
        data = FileTreeData(record, health)

        # Build label
        label = self._build_label(record, health, selected=False)

        if record.is_directory:
            node = parent_node.add(label, data=data)
            self._node_map[record.mft_index] = node
            for child in sorted(record.children, key=lambda c: (not c.is_directory, c.name.lower())):
                self._populate_node(node, child)
        else:
            node = parent_node.add_leaf(label, data=data)
            self._node_map[record.mft_index] = node

    def _build_label(
        self, record: FileRecord, health: FileHealth | None, selected: bool,
    ) -> Text:
        """Build the display label for a tree node."""
        check = "[bold green]\u2714[/]" if selected else "[dim]\u25CB[/]"
        icon = "\U0001F4C1" if record.is_directory else "\U0001F4C4"
        size_str = f"  {_human_size(record.size)}" if not record.is_directory else ""

        health_str = ""
        if health and not record.is_directory:
            health_str = f" [{health.style}]{health.indicator}[/]"
            if health.status == HealthStatus.PARTIAL:
                health_str += f" [{health.style}]{health.coverage_pct:.0f}%[/]"

        flags_str = ""
        if getattr(record, 'is_compressed', False):
            flags_str += " [yellow]\\[C][/]"
        if getattr(record, 'is_encrypted', False):
            flags_str += " [red]\\[E][/]"
        if record.is_deleted:
            flags_str += " [red]\\[DEL][/]"

        markup = f"{check} {icon} {record.name}{size_str}{health_str}{flags_str}"
        return Text.from_markup(markup)

    def _update_node_label(self, mft_index: int) -> None:
        """Refresh a node's label after selection change."""
        node = self._node_map.get(mft_index)
        if node is None or node.data is None:
            return
        ftd: FileTreeData = node.data
        node.set_label(self._build_label(ftd.record, ftd.health, ftd.selected))

    # --- Selection logic ---

    def _toggle_record(self, mft_index: int, force: bool | None = None) -> None:
        """Toggle or set selection for a single record."""
        node = self._node_map.get(mft_index)
        if node is None or node.data is None:
            return
        ftd: FileTreeData = node.data
        new_state = force if force is not None else (not ftd.selected)

        if new_state == ftd.selected:
            return

        ftd.selected = new_state
        if new_state:
            self.selected_indices.add(mft_index)
        else:
            self.selected_indices.discard(mft_index)

        self._update_node_label(mft_index)

    def _toggle_subtree(self, record: FileRecord, state: bool) -> None:
        """Set selection for a record and all descendants."""
        self._toggle_record(record.mft_index, force=state)
        for child in record.children:
            self._toggle_subtree(child, state)

    def _recompute_selection_stats(self) -> None:
        """Recompute selected count, size, and bytes-to-read."""
        count = 0
        size = 0
        to_read = 0
        for idx in self.selected_indices:
            rec = self.dir_tree.all_records.get(idx)
            if rec and not rec.is_directory:
                count += 1
                size += rec.size
                h = self.health_map.get(idx)
                if h:
                    to_read += h.bytes_to_read
        self.selected_count = count
        self.selected_size = size
        self.bytes_to_read = to_read
        self._update_status_bar()

    def _update_status_bar(self) -> None:
        bar = self.query_one("#status-bar", Static)
        mft_str = f"MFT: {self.mft_coverage_pct:.1f}%"
        if self.mft_coverage_pct < 100.0:
            mft_str += " (incomplete)"
        bar.update(
            f" {mft_str}  |  "
            f"Files: {self.dir_tree.total_files}  "
            f"Dirs: {self.dir_tree.total_dirs}  |  "
            f"Selected: {self.selected_count} files, {_human_size(self.selected_size)}  |  "
            f"To read: {_human_size(self.bytes_to_read)}  |  "
            f"\\[space]select  \\[r]ecover  \\[s]ave  \\[l]oad  \\[q]uit"
        )

    # --- Info panel ---

    def _update_info_panel(self, record: FileRecord) -> None:
        panel = self.query_one("#info-content", Static)
        health = self.health_map.get(record.mft_index)

        lines = [
            f"[b]Path:[/b] {record.full_path}",
            f"[b]Size:[/b] {_human_size(record.size)}",
            f"[b]Type:[/b] {'Directory' if record.is_directory else 'File'}",
        ]

        if record.created:
            lines.append(f"[b]Created:[/b] {record.created:%Y-%m-%d %H:%M}")
        if record.modified:
            lines.append(f"[b]Modified:[/b] {record.modified:%Y-%m-%d %H:%M}")

        if not record.is_directory:
            if record.resident_data is not None:
                lines.append("[b]Storage:[/b] Resident (in MFT)")
            elif record.data_runs:
                lines.append(f"[b]Fragments:[/b] {len(record.data_runs)} data run(s)")
            else:
                lines.append("[b]Storage:[/b] No data runs")

        if health and not record.is_directory:
            lines.append("")
            lines.append(f"[b]Coverage:[/b] {health.coverage_pct:.1f}%")
            lines.append(f"[b]In image:[/b] {_human_size(health.bytes_in_image)}")
            lines.append(f"[b]To read:[/b] {_human_size(health.bytes_to_read)}")
            lines.append(f"[b]Status:[/b] {health.indicator} {health.status.value}")

        flags = []
        if getattr(record, 'is_compressed', False):
            flags.append("[yellow]Compressed[/yellow]")
        if getattr(record, 'is_encrypted', False):
            flags.append("[red]Encrypted[/red]")
        if record.is_deleted:
            flags.append("[red]DELETED[/red]")
        if flags:
            lines.append("")
            lines.append("[b]Flags:[/b] " + ", ".join(flags))

        panel.update("\n".join(lines))

    # --- Event handlers ---

    @on(SelectableTree.NodeHighlighted)
    def on_node_highlighted(self, event: SelectableTree.NodeHighlighted) -> None:
        if event.node.data is not None:
            ftd: FileTreeData = event.node.data
            self._update_info_panel(ftd.record)

    @on(SelectableTree.SelectionRequested)
    def on_selection_requested(self, event: SelectableTree.SelectionRequested) -> None:
        self.action_toggle_select()

    def action_toggle_select(self) -> None:
        tree = self.query_one("#file-tree", SelectableTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        ftd: FileTreeData = node.data
        new_state = not ftd.selected
        if ftd.record.is_directory:
            self._toggle_subtree(ftd.record, new_state)
        else:
            self._toggle_record(ftd.record.mft_index, force=new_state)
        self._recompute_selection_stats()

    def action_select_all(self) -> None:
        for record in self.dir_tree.all_records.values():
            self._toggle_record(record.mft_index, force=True)
        self._recompute_selection_stats()

    def action_unselect_all(self) -> None:
        for idx in list(self.selected_indices):
            self._toggle_record(idx, force=False)
        self._recompute_selection_stats()

    def action_start_recovery(self) -> None:
        if not self.selected_indices:
            self.notify("No files selected.", severity="warning")
            return
        self._recovery_requested = True
        self.exit(result=self.selected_indices)

    def action_save_selection(self) -> None:
        if not self.selected_indices:
            self.notify("Nothing selected to save.", severity="warning")
            return
        path = Path(self.work_dir) / "selection.json"
        try:
            export_selection(self.selected_indices, self.dir_tree, path)
            self.notify(f"Selection saved to {path}")
        except OSError as e:
            self.notify(f"Save failed: {e}", severity="error")

    def action_load_selection(self) -> None:
        path = Path(self.work_dir) / "selection.json"
        if not path.exists():
            self.notify(f"No selection file found at {path}", severity="warning")
            return
        try:
            loaded = import_selection(path)
            # Clear current selection
            for idx in list(self.selected_indices):
                self._toggle_record(idx, force=False)
            # Apply loaded selection (expand directories)
            expanded = collect_selection_with_children(loaded, self.dir_tree)
            for idx in expanded:
                self._toggle_record(idx, force=True)
            self._recompute_selection_stats()
            self.notify(f"Loaded {len(loaded)} entries from {path}")
        except Exception as e:
            self.notify(f"Load failed: {e}", severity="error")

    def action_search(self) -> None:
        search_bar = self.query_one("#search-bar")
        search_bar.add_class("visible")
        self.query_one("#search-input", Input).focus()

    def action_cancel_search(self) -> None:
        search_bar = self.query_one("#search-bar")
        search_bar.remove_class("visible")
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.query_one("#file-tree", SelectableTree).focus()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip().lower()
        if not query:
            self.action_cancel_search()
            return

        # Find and expand path to first matching node
        tree = self.query_one("#file-tree", SelectableTree)
        for mft_idx, node in self._node_map.items():
            if node.data is None:
                continue
            ftd: FileTreeData = node.data
            if query in ftd.record.name.lower():
                # Expand ancestors
                parent = node.parent
                while parent is not None:
                    parent.expand()
                    parent = parent.parent
                tree.select_node(node)
                tree.scroll_to_node(node)
                self.action_cancel_search()
                return

        self.notify(f"No match for '{event.value}'", severity="warning")


def run_browser(
    dir_tree: DirectoryTree,
    mapfile: Mapfile | None = None,
    cluster_size: int = 4096,
    partition_offset: int = 0,
    device_name: str = "",
    image_path: str = "",
    work_dir: str = ".",
    mft_coverage_pct: float = 100.0,
) -> set[int] | None:
    """Launch the TUI browser and return selected MFT indices, or None if quit."""
    app = RecoveryBrowser(
        dir_tree=dir_tree,
        mapfile=mapfile,
        cluster_size=cluster_size,
        partition_offset=partition_offset,
        device_name=device_name,
        image_path=image_path,
        work_dir=work_dir,
        mft_coverage_pct=mft_coverage_pct,
    )
    result = app.run()
    if app._recovery_requested and isinstance(result, set):
        return result
    return None
