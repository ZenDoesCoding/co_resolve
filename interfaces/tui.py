import os
import re
import subprocess
import logging
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
import pyperclip
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container, ScrollableContainer
from textual.widgets import Tree, Input, Static, Button, Switch, Label
from textual.widget import Widget
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme
from rich.text import Text
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.geometry import Size
from utils.config_manager import ConfigManager, yaml_config

THEMES = [
    "ansi-dark", "ansi-light", "atom-one-dark", "atom-one-light",
    "catppuccin-frappe", "catppuccin-latte", "catppuccin-macchiato", "catppuccin-mocha",
    "dracula", "flexoki", "gruvbox", "monokai", "nord",
    "rose-pine", "rose-pine-dawn", "rose-pine-moon",
    "solarized-dark", "solarized-light", "textual-dark", "textual-light", "tokyo-night"
]

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text)

def parse_markdown_markup(text: str):
    final_chars = []
    styles = [] # list of (start, end, style_name)
    
    idx = 0
    n = len(text)
    in_code = False
    in_bold = False
    
    code_start = 0
    bold_start = 0
    
    while idx < n:
        if text[idx] == '`':
            if in_code:
                styles.append((code_start, len(final_chars), "code"))
                in_code = False
            else:
                code_start = len(final_chars)
                in_code = True
            idx += 1
        elif idx + 1 < n and text[idx] == '*' and text[idx+1] == '*':
            if in_bold:
                styles.append((bold_start, len(final_chars), "bold"))
                in_bold = False
            else:
                bold_start = len(final_chars)
                in_bold = True
            idx += 2
        else:
            final_chars.append(text[idx])
            idx += 1
            
    if in_code:
        styles.append((code_start, len(final_chars), "code"))
    if in_bold:
        styles.append((bold_start, len(final_chars), "bold"))
        
    return "".join(final_chars), styles

class SelectableChatLog(Widget, can_focus=True):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "cursor_left", "Left", show=False),
        Binding("l", "cursor_right", "Right", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("y", "yank", "Copy", show=False),
        Binding("v", "visual_mode", "Visual Mode", show=False),
        Binding("escape", "exit_visual", "Exit Visual", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.all_lines: list[str] = []
        self.lines: list[str] = []
        self.cursor_line = 0
        self.show_misc = True
        self.visual_mode = False
        self.visual_start_line = 0
        self.auto_scroll = True
        self.is_streaming = False

    def write(self, content: str):
        self.is_streaming = False
        for line in content.splitlines():
            self.all_lines.append(line)
        self._rebuild_lines()

    def stream_chunk(self, chunk: str):
        if not getattr(self, "is_streaming", False):
            self.all_lines.append("[NORMAL_LINE]")
        self.is_streaming = True
        if not self.all_lines:
            self.all_lines.append("[NORMAL_LINE]")
            
        lines = chunk.split("\n")
        self.all_lines[-1] += lines[0]
        for line in lines[1:]:
            self.all_lines.append("[NORMAL_LINE]" + line)
            
        self._rebuild_lines()

    def _rebuild_lines(self):
        try:
            container = self.app.query_one("#chat-log-container", Vertical)
            was_at_bottom = container.scroll_offset.y + container.size.height >= container.virtual_size.height - 2
            if container.virtual_size.height == 0: # Startup
                was_at_bottom = True
        except Exception:
            was_at_bottom = True

        self.lines = []
        width = self.size.width if self.size.width > 0 else 80
        import re
        
        idx = 0
        n = len(self.all_lines)
        whitelist = ["Reasoning", "💭", "Final Answer", "Pipeline completed", "Pipeline finished", "[REASONING_LINE]", "[NORMAL_LINE]"]
        
        while idx < n:
            raw = self.all_lines[idx]
            display_raw = raw
            if not self.show_misc:
                display_raw = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", "", raw)
                if not any(k in display_raw for k in whitelist):
                    idx += 1
                    continue
            
            # Check if this line is a table row
            def is_table_row_func(line_str: str) -> bool:
                clean = line_str.replace("[NORMAL_LINE]", "").strip()
                clean = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", "", clean)
                return clean.startswith("|")
                
            if is_table_row_func(display_raw):
                # Consume all consecutive table lines
                table_lines = []
                while idx < n:
                    next_raw = self.all_lines[idx]
                    next_display = next_raw
                    if not self.show_misc:
                        next_display = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", "", next_raw)
                        if not any(k in next_display for k in whitelist):
                            break
                    if is_table_row_func(next_display):
                        clean_line = next_display.replace("[NORMAL_LINE]", "").strip()
                        table_lines.append(clean_line)
                        idx += 1
                    else:
                        break
                
                if len(table_lines) >= 2:
                    from rich.table import Table
                    from rich.box import ROUNDED
                    
                    header_line = table_lines[0]
                    headers = [h.strip() for h in header_line.split("|")[1:-1]]
                    
                    # Validate headers to avoid parsing separator rows or empty rows as headers
                    is_valid_header = len(headers) > 0 and not all(all(c in "|- : " for c in h) for h in headers)
                    
                    if is_valid_header:
                        t_width = max(40, width - 4)
                        table = Table(box=ROUNDED, width=t_width, padding=(0, 1), show_lines=True)
                        for h in headers:
                            table.add_column(h, style="bold cyan")
                            
                        start_row_idx = 1
                        if len(table_lines) > 1 and len(table_lines[1].strip()) > 0 and all(c in "|- : " for c in table_lines[1]):
                            start_row_idx = 2
                            
                        for r_line in table_lines[start_row_idx:]:
                            cells = [c.strip() for c in r_line.split("|")[1:-1]]
                            while len(cells) < len(headers):
                                cells.append("")
                            cells = cells[:len(headers)]
                            colorized_cells = [self._colorize(cell) for cell in cells]
                            table.add_row(*colorized_cells)
                            
                        with self.app.console.capture() as capture:
                            self.app.console.print(table)
                        table_str = capture.get()
                        
                        for line in table_str.splitlines():
                            self.lines.append(self._colorize("[NORMAL_LINE]" + line))
                    else:
                        for t_line in table_lines:
                            self.lines.append(self._colorize(t_line))
                else:
                    for t_line in table_lines:
                        self.lines.append(self._colorize(t_line))
            else:
                t = self._colorize(display_raw)
                from rich.text import Text
                wrapped_parts = list(t.wrap(self.app.console, width))
                if not wrapped_parts:
                    wrapped_parts = [t]
                self.lines.extend(wrapped_parts)
                idx += 1
            
        # Update virtual size for scrolling
        self.styles.height = len(self.lines)
        self.virtual_size = Size(width, len(self.lines))
        
        # Ensure cursor line is valid
        if self.cursor_line >= len(self.lines):
            self.cursor_line = max(0, len(self.lines) - 1)
            
        # Smart Auto Scroll
        if self.auto_scroll or not self.lines:
            if getattr(self, "is_streaming", False):
                self.cursor_line = max(0, len(self.lines) - 2) if len(self.lines) > 1 else 0
            else:
                self.cursor_line = len(self.lines) - 1
            try:
                container.scroll_end(animate=False)
                self.app.query_one("#scroll-arrow", Button).display = False
            except Exception:
                pass
        else:
            try:
                # Only show button if content exceeds screen height
                if len(self.lines) > container.size.height:
                    self.app.query_one("#scroll-arrow", Button).display = True
            except Exception:
                pass
            
        self.refresh()

    def set_show_misc(self, value: bool):
        self.show_misc = value
        self._rebuild_lines()
        try:
            self.app.query_one("#throbber", Static).display = not value
        except Exception:
            pass

    def _colorize(self, line: str) -> Text:
        # Check if theme is light (by name or luminance)
        theme_name = getattr(self.app, "theme", "dark").lower()
        is_light = "light" in theme_name or "latte" in theme_name or "day" in theme_name
        
        try:
            if not is_light and self.styles.background.luminance > 0.5:
                is_light = True
        except Exception:
            pass
            
        # Strip ANSI escape codes
        clean_line = strip_ansi(line)
        
        is_reasoning = False
        is_normal = False
        if "[REASONING_LINE]" in clean_line:
            clean_line = clean_line.replace("[REASONING_LINE]", "")
            is_reasoning = True
        elif "[NORMAL_LINE]" in clean_line:
            clean_line = clean_line.replace("[NORMAL_LINE]", "")
            is_normal = True
            
        # Parse markdown markup
        plain_text, markdown_styles = parse_markdown_markup(clean_line)
        t = Text(plain_text)
        
        # Apply base style
        if is_reasoning:
            t.style = "italic #5f27cd" if is_light else "italic #a29bfe"
            # Fallback to readable dark/light grays if the purple is too vibrant
            t.style = "italic #57606f" if is_light else "italic #b2bec3"
        elif is_normal:
            t.style = "#000000" if is_light else "#636e72"
        else:
            t.style = "#2d3436" if is_light else "#dfe6e9"
            
        # Apply markdown styles (with color adjustments based on theme)
        code_color = "#8e44ad" if is_light else "#d580ff"
        bold_color = "#d35400" if is_light else "#f39c12"
        
        for start, end, style_name in markdown_styles:
            if style_name == "code":
                t.stylize(Style(color=code_color, bold=True), start, end)
            elif style_name == "bold":
                t.stylize(Style(color=bold_color, bold=True), start, end)
                
        plain = t.plain
        if "ERROR:" in plain:
            t.stylize("bold #ff4d4d" if is_light else "bold #ff6b6b")
        elif "INFO:" in plain:
            t.stylize("bold #0984e3" if is_light else "bold #74b9ff")
        elif "WARNING:" in plain:
            t.stylize("bold #e17055" if is_light else "bold #fdcb6e")
        elif "Reasoning" in plain or "💭" in plain:
            t.stylize("bold #6c5ce7" if is_light else "bold #a29bfe")
        elif plain.strip().startswith(">"):
            t.stylize("italic dim")
        elif plain.strip().startswith("|"):
            pipe_style = "bold #0984e3" if is_light else "bold #74b9ff"
            stripped = plain.strip()
            is_separator = all(c in "|- : " for c in stripped)
            if is_separator:
                t.stylize("dim #0984e3" if is_light else "dim #74b9ff")
            else:
                for i, char in enumerate(plain):
                    if char == "|":
                        t.stylize(pipe_style, i, i + 1)
            
        # Highlight comments (lines starting with # or comments in lines) in green/italic
        if "#" in plain:
            idx = plain.find("#")
            if idx == 0 and plain.startswith("# ERROR:"):
                pass
            else:
                t.stylize("italic #27ae60" if is_light else "italic #2ecc71", idx, len(plain))
        # Highlight command lines (pytest, python, pip, git, etc.) in bold yellow/orange
        elif re.search(r"\b(pytest|python|pip|git|cd|cat|sed)\b", plain):
            t.stylize("bold #d35400" if is_light else "bold #f39c12")

        return t


    def render_line(self, y: int) -> Strip:
        scroll_y = self.scroll_offset.y
        scroll_x = self.scroll_offset.x
        actual_y = y + scroll_y
        
        if actual_y >= len(self.lines):
            return Strip([])  # Let the widget background show through
            
        line_obj = self.lines[actual_y]
        from rich.text import Text
        if isinstance(line_obj, Text):
            line = line_obj.copy()
        else:
            line = self._colorize(str(line_obj))
        
        # Get widget background color
        bg_color = self.styles.background.hex6
        
        # Fallback if background evaluates to black but theme is light
        theme_name = getattr(self.app, "theme", "dark").lower()
        is_light = "light" in theme_name or "latte" in theme_name or "day" in theme_name
        if is_light and bg_color == "#000000":
            bg_color = "#eff1f5" # Default Latte surface color!
        
        # Determine if line is in selection
        is_selected = False
        if getattr(self, "visual_mode", False):
            start = min(self.visual_start_line, self.cursor_line)
            end = max(self.visual_start_line, self.cursor_line)
            if start <= actual_y <= end:
                is_selected = True
                
        # Cursor style
        if actual_y == self.cursor_line:
            # Highlight with a contrasting color
            line.stylize(Style(bgcolor="#2e3b4e", color="white"))
        elif is_selected:
            # Highlight selection range in visual mode
            line.stylize(Style(bgcolor="#34495e", color="white"))
        else:
            # Set background to match widget to avoid black background artifacts
            line.stylize(Style(bgcolor=bg_color))
            
        # Slice for horizontal scroll
        visible_line = line[scroll_x : scroll_x + self.size.width]
        
        # Render to segments
        segments = list(visible_line.render(self.app.console))
        
        # Pad to full width to ensure highlight extends to the right
        length = sum(len(s.text) for s in segments)
        if length < self.size.width:
            from rich.segment import Segment
            pad_color = "#2e3b4e" if actual_y == self.cursor_line else bg_color
            segments.append(Segment(" " * (self.size.width - length), Style(bgcolor=pad_color)))
            
        return Strip(segments)

    def on_click(self, event):
        self.cursor_line = event.y + self.scroll_offset.y
        if self.cursor_line >= len(self.lines):
            self.cursor_line = len(self.lines) - 1
        self.cursor_line = max(0, self.cursor_line)
        self.refresh()
        self.focus()

    def on_key(self, event) -> None:
        if event.key == "g":
            if getattr(self, "_last_key", "") == "g":
                self.action_scroll_home()
                self._last_key = ""
                event.prevent_default()
                event.stop()
            else:
                self._last_key = "g"
        else:
            self._last_key = ""

    def action_cursor_down(self):
        if self.cursor_line < len(self.lines) - 1:
            self.cursor_line += 1
            self.scroll_to_cursor()
            self.refresh()
            
            # Hide New Messages button if we reached the bottom
            if self.cursor_line >= len(self.lines) - 1:
                try:
                    self.app.query_one("#scroll-arrow", Button).display = False
                except Exception:
                    pass

    def action_cursor_up(self):
        self.auto_scroll = False
        if self.cursor_line > 0:
            self.cursor_line -= 1
            self.scroll_to_cursor()
            self.refresh()

    def action_cursor_left(self):
        if self.scroll_offset.x > 0:
            self.scroll_to(x=self.scroll_offset.x - 1)
            self.refresh()

    def action_cursor_right(self):
        self.scroll_to(x=self.scroll_offset.x + 1)
        self.refresh()

    def action_scroll_home(self):
        self.cursor_line = 0
        self.scroll_home()
        self.refresh()

    def action_scroll_end(self):
        self.cursor_line = len(self.lines) - 1
        self.scroll_end()
        self.refresh()

    def scroll_to_cursor(self):
        try:
            container = self.app.query_one("#chat-log-container", Vertical)
            # Ensure cursor is visible in container
            if self.cursor_line < container.scroll_offset.y:
                container.scroll_to(y=self.cursor_line, animate=False)
            elif self.cursor_line >= container.scroll_offset.y + container.size.height:
                container.scroll_to(y=self.cursor_line - container.size.height + 1, animate=False)
        except Exception:
            pass
        self.refresh()

    def action_visual_mode(self):
        self.visual_mode = True
        self.visual_start_line = self.cursor_line
        self.app.notify("Visual Mode (Lines)", severity="information")
        self.refresh()

    def action_exit_visual(self):
        if getattr(self, "visual_mode", False):
            self.visual_mode = False
            self.app.notify("Visual Mode disabled", severity="information")
            self.refresh()

    def action_yank(self):
        if getattr(self, "visual_mode", False):
            start = min(self.visual_start_line, self.cursor_line)
            end = max(self.visual_start_line, self.cursor_line)
            selected_lines = [self.lines[i] for i in range(start, end + 1)]
            selected_text = "\n".join(str(line) for line in selected_lines)
            self.visual_mode = False # Exit visual mode after yank
            self.refresh()
            count = end - start + 1
        else:
            if 0 <= self.cursor_line < len(self.lines):
                selected_text = self.lines[self.cursor_line]
                count = 1
            else:
                return
                
        try:
            import pyperclip
            pyperclip.copy(selected_text)
            self.app.notify(f"Copied {count} line(s) to clipboard!", severity="information")
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error")

    def clear(self):
        self.all_lines = []
        self.lines = []
        self.cursor_line = 0
        self.virtual_size = Size(80, 0)
        self.refresh()

    def focus_line(self, line_index: int):
        if 0 <= line_index < len(self.lines):
            self.cursor_line = line_index
            self.scroll_to_cursor()
            self.refresh()
            self.focus()
    def on_mouse_scroll_down(self, event):
        try:
            container = self.app.query_one("#chat-log-container", Vertical)
            container.scroll_down(animate=False)
            
            # Hide button if we reached bottom
            if container.scroll_offset.y + container.size.height >= container.virtual_size.height:
                self.app.query_one("#scroll-arrow", Button).display = False
        except Exception:
            pass

    def on_mouse_scroll_up(self, event):
        self.auto_scroll = False
        try:
            container = self.app.query_one("#chat-log-container", Vertical)
            container.scroll_up(animate=False)
        except Exception:
            pass




# ---------------------------------------------------------------------------
# Other Widgets
# ---------------------------------------------------------------------------

class LogQueueHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        msg = self.format(record)
        self.callback(msg)

class HistoryTree(Tree):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

class CommandInput(Input):
    def on_key(self, event) -> None:
        popup = self.app.query_one("#commands-popup", Static)
        if popup.display and event.key in ("down", "up"):
            event.prevent_default()
            event.stop()
            self.app._navigating_options = True
            if self.app.filtered_options:
                if event.key == "down":
                    self.app.selected_option_index = (self.app.selected_option_index + 1) % len(self.app.filtered_options)
                elif event.key == "up":
                    self.app.selected_option_index = (self.app.selected_option_index - 1) % len(self.app.filtered_options)
                self.app.update_popup_display()
            self.app._navigating_options = False
            return

        if event.key in ("enter", "return") and popup.display and getattr(self.app, "filtered_options", None):
            idx = getattr(self.app, "selected_option_index", 0)
            if 0 <= idx < len(self.app.filtered_options):
                choice = self.app.filtered_options[idx]
                self.value = choice
                self.cursor_position = len(choice)
                popup.display = False
                self.app._suppress_popup = True
                # Do NOT prevent default or stop; let it submit this new value!
                return

        if event.key == "escape" and popup.display:
            popup.display = False
            event.prevent_default()
            event.stop()
            return

        if event.key == "tab":
            event.prevent_default()
            event.stop()
            self.app.action_autocomplete()
        elif event.key == "alt+backspace":
            event.prevent_default()
            event.stop()
            val = self.value
            pos = self.cursor_position
            if pos == 0:
                return
            left = val[:pos]
            left_stripped = left.rstrip()
            if not left_stripped:
                self.value = val[pos:]
                self.cursor_position = 0
                return
            last_space = left_stripped.rfind(" ")
            if last_space == -1:
                self.value = val[pos:]
                self.cursor_position = 0
            else:
                self.value = val[:last_space + 1] + val[pos:]
                self.cursor_position = last_space + 1

class KeysScreen(ModalScreen):
    CSS = """
    KeysScreen {
        align: center middle;
        background: $background 50%;
    }
    #keys-container {
        width: 65%;
        height: 70%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #keys-content {
        height: 1fr;
        overflow-y: scroll;
    }
    #keys-hint {
        dock: bottom;
        background: $error;
        color: white;
        text-align: center;
        text-style: bold;
        padding: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "close_keys", "Close"),
        Binding("escape", "close_keys", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="keys-container"):
            yield Static(
                "Available Commands & Keys\n\n"
                "Commands (type in Chat Input):\n"
                "  /config        - Open configuration file in editor\n"
                "  /keys          - Show this keys window\n"
                "  /theme [name]  - Toggle or choose a theme\n"
                "  /debug         - Toggle debug mode on/off\n"
                "  /search [term] - Filter chat log (empty to reset)\n"
                "  /trigger       - Trigger empty commit in test repo\n"
                "  /approve       - Approve HitL action\n"
                "  /reject        - Reject HitL action\n"
                "  /approve_knowledge - Approve pending knowledge candidates\n"
                "  /reject_knowledge  - Discard pending knowledge candidates\n"
                "  /quit          - Exit the application\n\n"
                "Chat Window Keys (focus with Ctrl+S):\n"
                "  j / ↓          - Move cursor down\n"
                "  k / ↑          - Move cursor up\n"
                "  g              - Jump to top\n"
                "  G              - Jump to bottom\n"
                "  v              - Toggle Visual selection mode\n"
                "  y              - Copy selection to clipboard & exit\n"
                "  Esc            - Cancel visual mode\n\n"
                "Global Keys:\n"
                "  Ctrl+S         - Toggle focus Chat Window / Input\n"
                "  Ctrl+F         - Focus input and pre-fill /search\n"
                "  Alt+Backspace  - Delete last word in input\n"
                "  Tab            - Autocomplete command/theme\n",
                id="keys-content")
            yield Static("q to close this window", id="keys-hint")

    def action_close_keys(self):
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class CoResolveTUI(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #left-pane {
        width: 40%;
        border-right: solid $border;
    }
    #token-container {
        height: 3;
        align: left middle;
    }
    #token-counter {
        width: auto;
        margin-left: 2;
    }
    #session-timer {
        width: 1fr;
        text-align: right;
        margin-right: 2;
    }
    #right-pane {
        width: 60%;
    }
    #chat-log-container {
        height: 1fr;
        overflow-y: scroll;
    }
    #chat-log {
        background: $surface;
    }
    #throbber {
        display: none;
        color: $accent;
        text-style: italic;
        margin-left: 1;
        margin-top: 1;
        margin-bottom: 1;
    }
    Input {
        margin-bottom: 1;
    }
    #commands-popup {
        background: $surface;
        border: tall $primary;
        padding: 0 1;
        display: none;
        max-height: 10;
        overflow-y: scroll;
    }
    #scroll-arrow {
        display: none;
        background: $accent;
        color: white;
        margin-bottom: 0;
        width: 100%;
    }
    .pane-header {
        background: $boost;
        padding: 0 1;
        text-style: bold;
        align: left middle;
        height: 3;
        border-bottom: solid $border;
    }
    #chat-title {
        width: 1fr;
        align: left middle;
    }
    #misc-toggle-container {
        width: auto;
        align: right middle;
    }
    #misc-toggle-container Label {
        margin-right: 1;
        align: left middle;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "toggle_focus", "Toggle Focus"),
        Binding("ctrl+f", "search", "Search"),
    ]

    def __init__(self):
        super().__init__()
        from datetime import datetime
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.history_tree = HistoryTree("Agent Pipeline Runs")
        self.history_tree.root.expand()
        self.chat_log = SelectableChatLog(id="chat-log")
        self.chat_input = CommandInput(placeholder="Type a command (e.g. /config) | Ctrl+S = focus chat", id="chat-input")
        self.current_run_node = None
        self.current_step_node = None
        self.filtered_options = []
        self.selected_option_index = 0
        self.elapsed_seconds = 0.0
        self.blocked_seconds = 0.0
        self.last_timer_tick = None
        self.rate_limit_paused = False
        self.summary_rendered = False
        self.all_messages: list[str] = []
        self.session_dirty = False
        self.in_final_answer = False
        self.has_streamed_table = False
        self.skip_table_lines = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left-pane"):
                yield Static("History (Agent Pipeline)", classes="pane-header")
                yield self.history_tree
                yield Static("Token Usage (Session)", classes="pane-header")
                with Horizontal(id="token-container"):
                    yield Static("In: 0 | Out: 0 | Calls: 0", id="token-counter")
                    yield Static("00:00:00", id="session-timer")
            with Vertical(id="right-pane"):
                with Horizontal(classes="pane-header"):
                    yield Static("Chat Window", id="chat-title")
                    with Horizontal(id="misc-toggle-container"):
                        yield Label("Misc Info")
                        yield Switch(value=yaml_config.get("tui", {}).get("show_misc_info", True), id="toggle-misc")
                with Vertical(id="chat-log-container"):
                    yield self.chat_log
                yield Button("↓ New Messages ↓", id="scroll-arrow")
                yield Static(" Idle", id="throbber")
                yield Static("", id="commands-popup")
                yield self.chat_input

    def safe_call_from_thread(self, fn, *args, **kwargs):
        try:
            self.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            pass

    def send_backend_request(self, path: str, payload: dict) -> bool:
        import urllib.request
        import json
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:8000/api/{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=1.0) as response:
                return response.status == 200
        except Exception:
            return False

    def update_arc_display(self):
        from utils.active_arc import get_active_arc
        active_arc = get_active_arc()
        arc_names = {
            1: "basic_arc",
            2: "openai_arc",
            3: "google_arc"
        }
        name = arc_names.get(active_arc, f"ARC {active_arc}")
        try:
            self.query_one("#chat-title", Static).update(f"Chat Window (Backend: {name})")
        except Exception:
            pass


    def on_mount(self):
        root_logger = logging.getLogger()
        # Remove existing handlers (like StreamHandlers) to prevent logs clobbering the TUI
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)
            
        handler = LogQueueHandler(self.handle_log)
        formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        
        # Start throbber timer
        self.spinner_index = 0
        self.throbber_status = "Idle"
        self.set_interval(0.1, self.animate_spinner)
        
        self.timer_running = False
        self.set_interval(1.0, self.update_timer)
        self.set_interval(2.0, self.save_session_if_dirty)
        
        # We will start Uvicorn as a subprocess instead of a thread to avoid event loop conflicts.
        # So we don't need to intercept its logs here anymore.

        self.theme = yaml_config["tui"].get("theme", "textual-dark")

        show_misc = yaml_config.get("tui", {}).get("show_misc_info", True)
        self.chat_log.set_show_misc(show_misc)

        self.update_arc_display()
        self.chat_log.write("Co-Resolve Agent Dashboard Started.")
        self.chat_log.write("Type / to see available commands. Ctrl+S to focus chat log.")
        self.chat_input.focus()

        import subprocess
        import threading
        
        self.server_process = None

        def is_port_free(port: int) -> bool:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", port))
                    return True
                except OSError:
                    return False

        def run_server():
            if not is_port_free(8000):
                self.safe_call_from_thread(
                    self.chat_log.write,
                    "WARNING: Port 8000 is already in use. Skipping Uvicorn startup — webhook server may already be running."
                )
                return
            # Determine the correct app module (api.py takes priority over app.py)
            import os as _os
            if _os.path.exists("interfaces/api.py"):
                app_module = "interfaces.api:app"
            else:
                app_module = "interfaces.app:app"
            try:
                process = subprocess.Popen(
                    ["venv/bin/uvicorn", app_module, "--host", "0.0.0.0", "--port", "8000"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                self.server_process = process

                for line in iter(process.stdout.readline, ""):
                    # Process log via TUI handler to update tokens/throbber
                    self.handle_log(line.strip())

                process.stdout.close()
                process.wait()
            except Exception as e:
                self.safe_call_from_thread(self.chat_log.write, f"Failed to start Uvicorn: {e}")

        threading.Thread(target=run_server, daemon=True, name="uvicorn-subprocess").start()
        self.chat_log.write("Uvicorn server starting in subprocess on port 8000...")


        def run_ngrok_check():
            import urllib.request
            import urllib.error
            import json
            try:
                req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
                with urllib.request.urlopen(req, timeout=1.0) as response:
                    data = json.loads(response.read().decode())
                    tunnels = data.get("tunnels", [])
                    found = False
                    for tunnel in tunnels:
                        addr = tunnel.get("config", {}).get("addr", "")
                        public_url = tunnel.get("public_url", "")
                        if "8000" in addr:
                            self.safe_call_from_thread(
                                self.chat_log.write,
                                f"INFO: Active ngrok tunnel detected: {public_url} -> {addr}"
                            )
                            found = True
                            break
                    if not found:
                        self.safe_call_from_thread(
                            self.chat_log.write,
                            "WARNING: ngrok is running, but no active tunnel was found targeting port 8000. GitHub webhooks may not reach Uvicorn."
                        )
            except Exception:
                self.safe_call_from_thread(
                    self.chat_log.write,
                    "WARNING: ngrok is not running locally. GitHub webhooks will not be delivered to Uvicorn. Run: ngrok http 8000"
                )

        threading.Thread(target=run_ngrok_check, daemon=True, name="ngrok-check").start()

    def save_session_if_dirty(self):
        if getattr(self, "session_dirty", False):
            self.session_dirty = False
            try:
                import os
                os.makedirs("sessions", exist_ok=True)
                import json
                import time

                # Calculate elapsed time
                elapsed = int(getattr(self, "elapsed_seconds", 0.0))
                blocked = int(getattr(self, "blocked_seconds", 0.0))

                payload = {
                    "messages": self.all_messages,
                    "total_input_tokens": getattr(self, "total_input_tokens", 0),
                    "total_output_tokens": getattr(self, "total_output_tokens", 0),
                    "total_api_calls": getattr(self, "total_api_calls", 0),
                    "elapsed_seconds": elapsed,
                    "blocked_seconds": blocked
                }

                with open(f"sessions/{self.session_id}.json", "w") as f:
                    json.dump(payload, f)
            except Exception:
                pass

    def on_unmount(self):
        self.save_session_if_dirty()
        if hasattr(self, "server_process") and self.server_process:
            try:
                self.server_process.terminate()
                self.server_process.wait(timeout=2)
            except Exception:
                try:
                    self.server_process.kill()
                except Exception:
                    pass

    def animate_spinner(self):
        try:
            throbber = self.query_one("#throbber", Static)
            if throbber.display:
                if self.throbber_status == "Idle":
                    frame = "⣿" # Static filled block for Idle
                else:
                    self.spinner_index = (self.spinner_index + 1) % len(SPINNER_FRAMES)
                    frame = SPINNER_FRAMES[self.spinner_index]
                throbber.update(f"{frame} {self.throbber_status}")
        except Exception:
            pass

    def update_timer(self):
        if not getattr(self, "timer_running", False):
            return
        import time
        now = time.time()
        last = getattr(self, "last_timer_tick", None)
        if last is None:
            last = now
        delta = now - last
        self.last_timer_tick = now

        if getattr(self, "rate_limit_paused", False):
            self.blocked_seconds = getattr(self, "blocked_seconds", 0.0) + delta
        else:
            self.elapsed_seconds = getattr(self, "elapsed_seconds", 0.0) + delta

        elapsed = int(self.elapsed_seconds)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        prefix = "⏱️ " if getattr(self, "rate_limit_paused", False) else ""
        timer_str = f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"
        try:
            self.query_one("#session-timer", Static).update(timer_str)
        except Exception:
            pass

    def clear_rate_limit_paused(self):
        self.rate_limit_paused = False
        self.update_timer()

    def handle_log(self, message: str):
        self.safe_call_from_thread(self._process_log, message)

    def _process_log(self, message: str):
        global yaml_config
        debug_mode = yaml_config["tui"].get("debug_mode", True)

        # Skip duplicate table lines if table was already streamed
        stripped_msg = strip_ansi(message).strip()
        if getattr(self, "skip_table_lines", False) and stripped_msg.startswith("|"):
            return

        # Reset table stream flags if a new run begins
        if "Starting agent pipeline" in stripped_msg:
            self.has_streamed_table = False
            self.skip_table_lines = False

        # State tracking for final answer
        # If a line starts with standard log prefix and does not contain Final Answer, reset
        is_new_log = any(stripped_msg.startswith(p) for p in ["INFO:", "WARNING:", "ERROR:"]) or (stripped_msg.startswith("[") and "]" in stripped_msg[:25])
        
        if is_new_log:
            if "Final Answer" in stripped_msg:
                self.in_final_answer = True
                if getattr(self, "has_streamed_table", False):
                    self.skip_table_lines = True
            else:
                self.in_final_answer = False
                self.skip_table_lines = False

        # Detect rate limit pauses
        if "Rate limit reached. Sleeping for" in message:
            try:
                import re
                match = re.search(r"Sleeping for ([\d\.]+) seconds", message)
                if match:
                    sleep_time = float(match.group(1))
                    self.rate_limit_paused = True
                    self.update_timer()
                    self.set_timer(sleep_time + 0.5, self.clear_rate_limit_paused)
            except Exception:
                pass
        elif any(k in message for k in ["LLM Tool Call:", "HTTP Request:", "Tool Call:", "Updated file"]):
            if getattr(self, "rate_limit_paused", False):
                self.rate_limit_paused = False
                self.update_timer()

        # Update throbber status if misc mode is OFF
        if not self.chat_log.show_misc:
            if "Detected failed workflow" in message:
                self.throbber_status = "Workflow detected..."
                self.query_one("#throbber", Static).display = True
            elif "Starting agent" in message:
                self.throbber_status = "Starting..."
                self.query_one("#throbber", Static).display = True
            elif "Reasoning" in message:
                self.throbber_status = "Thinking..."
                self.query_one("#throbber", Static).display = True
            elif "Tool Call: run_python_test_file" in message:
                self.throbber_status = "Testing..."
                self.query_one("#throbber", Static).display = True
            elif "Tool Call: apply_file_modification" in message:
                self.throbber_status = "Modifying file..."
                self.query_one("#throbber", Static).display = True
            elif "Tool Call: get_repo_map" in message:
                self.throbber_status = "Mapping repo..."
                self.query_one("#throbber", Static).display = True
            elif "Pipeline completed" in message or "Fix resolved" in message:
                self.throbber_status = "Idle"

        # Handle API calls
        if "[API_CALL]" in message:
            if not hasattr(self, "total_api_calls"):
                self.total_api_calls = 0
            if not hasattr(self, "total_input_tokens"):
                self.total_input_tokens = 0
                self.total_output_tokens = 0
            self.total_api_calls += 1
            self.query_one("#token-counter", Static).update(f"In: {self.total_input_tokens} | Out: {self.total_output_tokens} | Calls: {self.total_api_calls}")
            return # Don't show in chat log

        # Handle token usage
        if "[TOKEN_USAGE]" in message:
            try:
                parts = message.split()
                idx = parts.index("[TOKEN_USAGE]")
                input_tokens = int(parts[idx+1])
                output_tokens = int(parts[idx+2])
                
                if not hasattr(self, "total_input_tokens"):
                    self.total_input_tokens = 0
                    self.total_output_tokens = 0
                
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                
                calls = getattr(self, "total_api_calls", 0)
                self.query_one("#token-counter", Static).update(f"In: {self.total_input_tokens} | Out: {self.total_output_tokens} | Calls: {calls}")
                
                # Log to file per session
                with open("session_tokens.log", "a") as f:
                    f.write(f"In: {input_tokens}, Out: {output_tokens}, Total In: {self.total_input_tokens}, Total Out: {self.total_output_tokens}\n")
            except Exception:
                pass
            return # Don't show in chat log

        if "[KNOWLEDGE_REVIEW]" in message:
            import json
            json_str = message.split("[KNOWLEDGE_REVIEW]", 1)[1]
            try:
                candidates = json.loads(json_str)
                self.chat_log.write("\n💡 **Knowledge Candidates Pending Approval:**")
                for c in candidates:
                    self.chat_log.write(f"- **Pattern:** {c.get('pattern', '')}")
                    self.chat_log.write(f"  **Problem:** {c.get('problem', '')}")
                    self.chat_log.write(f"  **Solution:** {c.get('solution', '')}")
                self.chat_log.write("Type `/approve_knowledge` to save or `/reject_knowledge` to discard.\n")
            except Exception as e:
                self.chat_log.write(f"Error parsing knowledge candidates: {e}")
            return

        if "[STREAM_CHUNK]" in message:
            chunk = message.split("[STREAM_CHUNK]", 1)[1]
            if "|" in chunk:
                self.has_streamed_table = True
            # Replace a special newline marker if we used one
            chunk = chunk.replace("\\n", "\n")
            self.chat_log.stream_chunk(chunk)
            return

        # Handle agent reasoning — display inline in chat.
        # Encrypted reasoning: only show in debug mode.
        # Unencrypted reasoning: always show (it's the real thinking).
        if "[AGENT_REASONING]" in message:
            try:
                # Unescape escaped newlines to support multiline subprocess output
                unescaped = message.replace("\\n", "\n")
                encrypted = "encrypted=True" in unescaped
                
                # Split off the header/prefix line(s) and extract reasoning text
                lines = unescaped.split("\n")
                header_idx = -1
                for i, line in enumerate(lines):
                    if "[AGENT_REASONING]" in line:
                        header_idx = i
                        break
                
                if header_idx != -1 and header_idx < len(lines) - 1:
                    reasoning_text = "\n".join(lines[header_idx+1:])
                else:
                    reasoning_text = "\n".join([l for l in lines if "[AGENT_REASONING]" not in l])
                
                reasoning_text = reasoning_text.strip()
                
                if encrypted and not debug_mode:
                    return  # Hidden in non-debug mode
                
                if not getattr(self, "_replaying", False):
                    self.all_messages.append(message)
                
                prefix = "🔒 [Encrypted Reasoning]" if encrypted else "💭 Reasoning"
                
                import re
                from rich.panel import Panel
                from rich.box import ROUNDED
                from rich.console import Console
                
                # Convert markdown bold/code blocks to Rich markup so Panel renders them natively
                # and line lengths after strip_ansi are perfectly preserved.
                markup = reasoning_text
                markup = markup.replace("[", "\\[").replace("]", "\\]")
                markup = re.sub(r'\*\*(.*?)\*\*', r'[bold]\1[/bold]', markup)
                markup = re.sub(r'`(.*?)`', r'[bold purple]\1[/bold purple]', markup)
                
                panel_content = f"[bold purple]{prefix}:[/bold purple]\n{markup}"
                panel = Panel(panel_content, box=ROUNDED, width=54)
                
                console = Console(width=54)
                with console.capture() as capture:
                    console.print(panel)
                panel_str = capture.get()
                
                for line in panel_str.splitlines():
                    self.chat_log.write("[REASONING_LINE]" + line)
                return
            except Exception:
                pass  # Fall through to normal display

        # Filter server logs
        if not yaml_config["tui"].get("show_server_logs", False):
            if "POST /api/webhook" in message or "Webhook received" in message:
                return

        allowed_keywords = [
            "Starting agent pipeline", 
            "Pipeline completed", 
            "Pipeline finished",
            "Fix resolved",
            "Final Answer",
            "[AGENT]", 
            "Running test in persistent sandbox", 
            "Updated file in sandbox", 
            "Tool Call:", 
            "LLM Tool Call:",
            "ERROR:"
        ]
        is_allowed = any(k in message for k in allowed_keywords) or message.strip().startswith("|") or getattr(self, "in_final_answer", False)

        display_message = message
        if "[AGENT]" in display_message:
            display_message = display_message.replace("[AGENT]", "**[AGENT]**")

        # Convert prefixes to Markdown syntax for Highlighting
        if display_message.startswith("ERROR:"):
            display_message = display_message.replace("ERROR:", "# ERROR:", 1)
        elif display_message.startswith("WARNING:"):
            display_message = display_message.replace("WARNING:", "*WARNING:*", 1)
        elif display_message.startswith("INFO:"):
            display_message = display_message.replace("INFO:", "**INFO:**", 1)

        # Update Chat Log first to get line index
        line_index = None
        if debug_mode or is_allowed:
            if not getattr(self, "_replaying", False):
                self.all_messages.append(message) # Save raw message for replay
            
            if is_allowed:
                if not any(display_message.startswith(prefix) for prefix in ["# ERROR:", "*WARNING:*", "**INFO:**"]) and not display_message.strip().startswith("|"):
                    from rich.panel import Panel
                    from rich.box import ROUNDED
                    from rich.console import Console
                    
                    panel = Panel(display_message, box=ROUNDED, width=54)
                    console = Console(width=54)
                    with console.capture() as capture:
                        console.print(panel)
                    panel_str = capture.get()
                    
                    for line in panel_str.splitlines():
                        self.chat_log.write("[NORMAL_LINE]" + line)
                else:
                    # If a log is allowed, write all lines with [NORMAL_LINE] prefix so they display
                    # EXCEPT for info and warning messages, which should be hidden in non-misc mode
                    for line in display_message.splitlines():
                        line_to_hide = any(line.startswith(p) for p in ["**INFO:**", "*WARNING:*"])
                        if line_to_hide:
                            self.chat_log.write(line)
                        else:
                            self.chat_log.write("[NORMAL_LINE]" + line)
            else:
                self.chat_log.write(display_message)
            line_index = len(self.chat_log.lines) - 1
            
            # For the scroll arrow, we can check if the cursor is at the bottom
            at_bottom = self.chat_log.cursor_line >= len(self.chat_log.lines) - 2
            if not at_bottom and not getattr(self, "_replaying", False):
                self.query_one("#scroll-arrow", Button).display = True

        # Mark session as dirty for deferred saving
        if not getattr(self, "_replaying", False):
            self.session_dirty = True

        # Update Tree (only if allowed)
        if is_allowed:
            if "Starting agent pipeline" in message:
                import time
                self.session_start_time = time.time()
                self.last_timer_tick = time.time()
                self.timer_running = True
                self.total_input_tokens = 0
                self.total_output_tokens = 0
                self.total_api_calls = 0
                self.elapsed_seconds = 0.0
                self.blocked_seconds = 0.0
                self.rate_limit_paused = False
                self.summary_rendered = False
                try:
                    self.query_one("#token-counter", Static).update("In: 0 | Out: 0 | Calls: 0")
                except Exception:
                    pass
                try:
                    self.query_one("#session-timer", Static).update("00:00:00")
                except Exception:
                    pass
                if self.current_run_node:
                    self.current_run_node.collapse()
                self.current_run_node = self.history_tree.root.add(message, expand=True, data=line_index)
                self.current_step_node = None
            elif "LLM Tool Call:" in message:
                if self.current_step_node:
                    self.current_step_node.collapse()
                if self.current_run_node:
                    self.current_step_node = self.current_run_node.add(message, expand=True, data=line_index)
            elif "Pipeline completed" in message or "Fix resolved" in message or "Pipeline finished" in message:
                self.timer_running = False
                if self.current_run_node:
                    self.current_run_node.add_leaf(message, data=line_index)
                
                # Render premium summary screen (only once)
                if not getattr(self, "summary_rendered", False):
                    self.summary_rendered = True
                    try:
                        from rich.panel import Panel
                        from rich.table import Table
                        from rich.box import ROUNDED
                        from rich.console import Console

                        # Format times
                        def format_time(secs):
                            secs = int(secs)
                            h = secs // 3600
                            m = (secs % 3600) // 60
                            s = secs % 60
                            return f"{h:02d}:{m:02d}:{s:02d}"

                        total_input = getattr(self, "total_input_tokens", 0)
                        total_output = getattr(self, "total_output_tokens", 0)
                        total_calls = getattr(self, "total_api_calls", 0)
                        
                        blocked_sec = getattr(self, "blocked_seconds", 0.0)
                        active_sec = getattr(self, "elapsed_seconds", 0.0)
                        total_sec = active_sec + blocked_sec

                        table = Table(box=None, show_header=False, pad_edge=False)
                        table.add_column("Metric", style="bold cyan")
                        table.add_column("Value", style="bold green")

                        table.add_row("  📥 Input Tokens (In)", f"{total_input:,}")
                        table.add_row("  📤 Output Tokens (Out)", f"{total_output:,}")
                        table.add_row("  📞 API Calls", f"{total_calls}")
                        table.add_row("  ⏱️ Total Duration", f"{format_time(total_sec)}")
                        table.add_row("  🟢 Active Execution Time", f"{format_time(active_sec)}")
                        table.add_row("  ⏳ Rate Limit Blocked Time", f"{format_time(blocked_sec)}")

                        panel = Panel(
                            table,
                            title="Pipeline Execution Summary",
                            title_align="center",
                            border_style="bold green",
                            box=ROUNDED,
                            width=55
                        )

                        console = Console(width=60)
                        with console.capture() as capture:
                            console.print(panel)
                        panel_str = capture.get()
                        
                        # Print it to the chat log
                        self.chat_log.write("") # blank line for spacing
                        for line in panel_str.splitlines():
                            self.chat_log.write("[NORMAL_LINE]" + line)
                        self.chat_log.write("") # blank line for spacing
                    except Exception as e:
                        self.chat_log.write(f"[NORMAL_LINE]Error rendering summary: {e}")
            else:
                if self.current_step_node:
                    self.current_step_node.add_leaf(message, data=line_index)
                elif self.current_run_node:
                    self.current_run_node.add_leaf(message, data=line_index)



    # ── Input events ────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed):
        popup = self.query_one("#commands-popup", Static)
        val = event.value

        last_val = getattr(self, "last_val", "")
        self.last_val = val

        if val == "/theme" and len(val) > len(last_val):
            self.chat_input.value = "/theme "
            self.chat_input.cursor_position = len("/theme ")
            return
        elif val == "/history" and len(val) > len(last_val):
            self.chat_input.value = "/history "
            self.chat_input.cursor_position = len("/history ")
            return
        elif val == "/session" and len(val) > len(last_val):
            self.chat_input.value = "/session "
            self.chat_input.cursor_position = len("/session ")
            return

        if getattr(self, "_suppress_popup", False):
            self._suppress_popup = False
            popup.display = False
            return

        if val.endswith("  "):
            self.chat_input.value = val[:-1]
            return

        if not getattr(self, "_navigating_options", False):
            self.selected_option_index = 0

        if val.startswith("/"):
            popup.display = True
            self.update_popup_display()
        else:
            popup.display = False
            self.filtered_options = []

    def update_popup_display(self):
        popup = self.query_one("#commands-popup", Static)
        val = self.chat_input.value
        if not val.startswith("/"):
            popup.display = False
            return

        from rich.text import Text
        
        if val.startswith("/history") or val.startswith("/session"):
            popup.styles.max_height = 10
            sessions = self._get_sessions()
            if val.startswith("/history"):
                prefix = "/history"
                search_term = val[9:].lower() if val.startswith("/history ") else val[8:].lower()
            else:
                prefix = "/session"
                search_term = val[9:].lower() if val.startswith("/session ") else val[8:].lower()
                
            filtered_sessions = [s for s in sessions if search_term in s.lower()]
            if not filtered_sessions:
                popup.update(Text("No matching sessions"))
                self.filtered_options = []
                return
                
            max_visible = 8
            visible_sessions = filtered_sessions[:max_visible]
            self.filtered_options = [f"{prefix} {s}" for s in visible_sessions]
            
            t = Text("Recent Sessions:\n")
            for i, s in enumerate(visible_sessions):
                option_text = f"  {prefix} {s}"
                if i == getattr(self, "selected_option_index", 0):
                    t.append(option_text + "\n", style="reverse bold #74b9ff")
                else:
                    t.append(option_text + "\n")
            if len(filtered_sessions) > max_visible:
                t.append("  ...\n")
            t.rstrip()
            popup.update(t)
            
        elif val.startswith("/theme"):
            popup.styles.max_height = 15
            search_term = val[7:].lower() if val.startswith("/theme ") else val[6:].lower()
            filtered_themes = [theme for theme in THEMES if search_term in theme.lower()]
            if not filtered_themes:
                popup.update(Text("No matching themes"))
                self.filtered_options = []
                return
                
            max_visible = 10
            visible_themes = filtered_themes[:max_visible]
            self.filtered_options = [f"/theme {theme}" for theme in visible_themes]
            
            t = Text("Available Themes:\n")
            for i, theme in enumerate(visible_themes):
                option_text = f"  /theme {theme}"
                if i == getattr(self, "selected_option_index", 0):
                    t.append(option_text + "\n", style="reverse bold #74b9ff")
                else:
                    t.append(option_text + "\n")
            if len(filtered_themes) > max_visible:
                t.append("  ...\n")
            t.rstrip()
            popup.update(t)

        elif val.startswith("/switch"):
            popup.styles.max_height = 10
            search_term = val[8:].strip() if val.startswith("/switch ") else val[7:].strip()
            
            backends = [
                ("2", "openai_arc (OpenAI SDK support and Google API support via translation layer)"),
                ("3", "google_arc (Only Google API support via interactions API)"),
                ("1", "basic_arc (Only OpenAI SDK support)")
            ]
            
            filtered_backends = [b for b in backends if not search_term or search_term in b[0] or search_term.lower() in b[1].lower()]
            if not filtered_backends:
                popup.update(Text("No matching backends"))
                self.filtered_options = []
                return
                
            self.filtered_options = [f"/switch {b[0]}" for b in filtered_backends]
            
            t = Text("Available Backends:\n")
            for i, (num, desc) in enumerate(filtered_backends):
                option_text = f"  /switch {num} - {desc}"
                if i == getattr(self, "selected_option_index", 0):
                    t.append(option_text + "\n", style="reverse bold #74b9ff")
                else:
                    t.append(option_text + "\n")
            t.rstrip()
            popup.update(t)

        else:
            popup.styles.max_height = 10
            commands = [
                ("/config", "Edit configuration"),
                ("/keys", "Show shortcuts"),
                ("/theme", "Toggle or choose theme"),
                ("/history", "List/Load past sessions"),
                ("/session", "Alias for /history"),
                ("/debug", "Toggle debug mode"),
                ("/search", "Filter chat log"),
                ("/approve", "Approve HitL fix"),
                ("/reject", "Reject HitL fix"),
                ("/trigger", "Trigger empty commit in test repo"),
                ("/switch", "Switch active backend (e.g. /switch 1 to 4)"),
                ("/approve_knowledge", "Approve pending knowledge"),
                ("/reject_knowledge", "Discard pending knowledge"),
                ("/quit", "Exit dashboard"),
            ]
            search_term = val[1:].lower()
            filtered = [c for c in commands if search_term in c[0][1:].lower()]
            self.filtered_options = [c[0] for c in filtered]
            
            if not filtered:
                popup.update(Text("No matching commands"))
                return
                
            t = Text("Available Commands:\n")
            for i, (cmd, desc) in enumerate(filtered):
                option_text = f"  {cmd} - {desc}"
                if i == getattr(self, "selected_option_index", 0):
                    t.append(option_text + "\n", style="reverse bold #74b9ff")
                else:
                    t.append(option_text + "\n")
            t.rstrip()
            popup.update(t)

    def on_input_submitted(self, event: Input.Submitted):
        global yaml_config
        command = event.value.strip()
        self.chat_input.value = ""

        if command == "/config":
            self.chat_log.write("Opening config file...")
            self.open_editor("config.yaml")
        elif command == "/approve":
            self.send_backend_request("hitl", {"action": "approve"})
            self.chat_log.write("Sent approval for HitL.")
        elif command == "/reject":
            self.send_backend_request("hitl", {"action": "reject"})
            self.chat_log.write("Sent rejection for HitL.")
        elif command.startswith("/switch"):
            parts = command.split()
            if len(parts) > 1:
                val = parts[1].strip().lower()
                arc_map = {
                    "openai_arc": 2, "2": 2,
                    "google_arc": 3, "3": 3,
                    "basic_arc": 1, "1": 1
                }
                arc_names = {
                    1: "basic_arc",
                    2: "openai_arc",
                    3: "google_arc"
                }
                if val in arc_map:
                    arc_num = arc_map[val]
                    name = arc_names[arc_num]
                    if self.send_backend_request("switch_arc", {"arc": arc_num}):
                        self.chat_log.write(f"Switched active backend to {name}.")
                        self.update_arc_display()
                    else:
                        # Fallback to local file update if backend server is offline
                        from utils.active_arc import set_active_arc
                        set_active_arc(arc_num)
                        self.chat_log.write(f"Switched active backend locally to {name} (backend server offline).")
                        self.update_arc_display()
                else:
                    self.chat_log.write("Usage: /switch <openai_arc|google_arc|basic_arc> or <1-3>")
            else:
                self.chat_log.write("Usage: /switch <openai_arc|google_arc|basic_arc> or <1-3>")
        elif command == "/approve_knowledge":
            from core.knowledge_manager import KnowledgeManager
            km = KnowledgeManager()
            km.commit_approved()
            self.chat_log.write("Approved and committed knowledge candidates.")
        elif command == "/reject_knowledge":
            from core.knowledge_manager import KnowledgeManager
            km = KnowledgeManager()
            km.discard_candidates()
            self.chat_log.write("Rejected and cleared knowledge candidates.")
        elif command == "/trigger":
            self.chat_log.write("Triggering empty commit in test repo...")
            try:
                import subprocess
                res1 = subprocess.run(
                    ["git", "-C", "/home/zen/codebase/vibe/co_resolve_testing", "commit", "--allow-empty", "-m", "trigger cli"],
                    capture_output=True, text=True
                )
                if res1.stdout:
                    self.chat_log.write(res1.stdout.strip())
                if res1.stderr:
                    self.chat_log.write(res1.stderr.strip())
                
                res2 = subprocess.run(
                    ["git", "-C", "/home/zen/codebase/vibe/co_resolve_testing", "push"],
                    capture_output=True, text=True
                )
                if res2.stdout:
                    self.chat_log.write(res2.stdout.strip())
                if res2.stderr:
                    self.chat_log.write(res2.stderr.strip())
                
            except Exception as e:
                self.chat_log.write(f"Failed to trigger commit: {e}")
        elif command == "/keys":
            self.push_screen(KeysScreen())
        elif command.startswith("/theme"):
            parts = command.split()
            if len(parts) > 1:
                new_theme = parts[1]
                if new_theme in THEMES:
                    self.theme = new_theme
                    self.chat_log.write(f"Theme changed to: {new_theme}")
                    yaml_config["tui"]["theme"] = new_theme
                    ConfigManager.save_config(yaml_config)
                else:
                    self.chat_log.write(f"Unknown theme: {new_theme}")
            else:
                current_theme = getattr(self, "theme", "textual-dark")
                new_theme = "textual-light" if current_theme == "textual-dark" else "textual-dark"
                self.theme = new_theme
                self.chat_log.write(f"Theme toggled to: {new_theme}")
                yaml_config["tui"]["theme"] = new_theme
                ConfigManager.save_config(yaml_config)
        elif command.startswith("/history") or command.startswith("/session"):
            parts = command.split()
            if len(parts) > 1:
                session_id = parts[1]
                self._load_session(session_id)
            else:
                sessions = self._get_sessions()
                if sessions:
                    self.chat_log.write("Recent Sessions:")
                    prefix = "/session" if command.startswith("/session") else "/history"
                    for s in sessions[:5]:
                        self.chat_log.write(f"  {prefix} {s}")
                else:
                    self.chat_log.write("No saved sessions found.")
        elif command == "/debug":
            current_debug = yaml_config["tui"].get("debug_mode", True)
            new_debug = not current_debug
            yaml_config["tui"]["debug_mode"] = new_debug
            ConfigManager.save_config(yaml_config)
            self.chat_log.write(f"Debug mode: {'ON' if new_debug else 'OFF'}")
        elif command.startswith("/search"):
            parts = command.split(maxsplit=1)
            if len(parts) > 1:
                query = parts[1].lower()
                self.chat_log.clear()
                matches = [msg for msg in self.all_messages if query in msg.lower()]
                self.chat_log.write(f"--- Search: '{query}' ({len(matches)} results) ---")
                for msg in matches:
                    self.chat_log.write(msg)
                self.chat_log.write("--- End of results. Type /search to reset. ---")
                self.chat_log.clear()
                for msg in self.all_messages:
                    self.chat_log.write(msg)
        elif command == "/quit":
            self.app.exit()
        elif command.startswith("/"):
            self.chat_log.write(f"Unknown command: {command}")
        elif command:
            self.chat_log.write(f"You: {command}")

    def _get_sessions(self) -> list[str]:
        import os
        if not os.path.exists("sessions"):
            return []
        files = [f for f in os.listdir("sessions") if f.endswith(".json")]
        # Sort by modification time, newest first
        files.sort(key=lambda x: os.path.getmtime(os.path.join("sessions", x)), reverse=True)
        return [f[:-5] for f in files]

    def _load_session(self, session_id: str):
        import json
        import os
        filepath = f"sessions/{session_id}.json"
        if not os.path.exists(filepath):
            self.chat_log.write(f"Session not found: {session_id}")
            return

        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            
            # Clear current state
            self.chat_log.clear()
            self.all_messages = []
            # Reset tree
            for child in list(self.history_tree.root.children):
                child.remove()
            self.current_run_node = None
            self.current_step_node = None
            
            # Load stats from data or default to 0
            self.total_input_tokens = data.get("total_input_tokens", 0)
            self.total_output_tokens = data.get("total_output_tokens", 0)
            self.total_api_calls = data.get("total_api_calls", 0)
            self.elapsed_seconds = data.get("elapsed_seconds", 0)
            
            try:
                self.query_one("#token-counter", Static).update(
                    f"In: {self.total_input_tokens} | Out: {self.total_output_tokens} | Calls: {self.total_api_calls}"
                )
            except Exception:
                pass

            try:
                hours = self.elapsed_seconds // 3600
                minutes = (self.elapsed_seconds % 3600) // 60
                seconds = self.elapsed_seconds % 60
                self.query_one("#session-timer", Static).update(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
            except Exception:
                pass

            # Replay messages
            messages = data.get("messages", [])
            self.chat_log.write(f"--- Replaying Session: {session_id} ({len(messages)} messages) ---")
            
            # Temporarily disable auto-save during replay to avoid overwriting or infinite loops!
            # But wait, we are loading a PAST session, so we should probably NOT overwrite it unless the user interacts!
            # Let's switch session_id to the loaded one!
            self.session_id = session_id
            
            for msg in messages:
                # We need to call _process_log but avoid saving it again!
                # Let's create a flag or just append to all_messages and call the rendering part!
                # Actually, if we just call _process_log, it will save it again!
                # Let's temporarily disable saving!
                self._replaying = True
                self._process_log(msg)
                self._replaying = False

            self.chat_log.write(f"--- End of Session Replay ---")
            
        except Exception as e:
            self.chat_log.write(f"Error loading session: {e}")

    def on_switch_changed(self, event: Switch.Changed):
        if event.switch.id == "toggle-misc":
            new_value = event.value
            self.chat_log.set_show_misc(new_value)
            
            # Save to config
            global yaml_config
            if "tui" not in yaml_config:
                yaml_config["tui"] = {}
            yaml_config["tui"]["show_misc_info"] = new_value
            ConfigManager.save_config(yaml_config)
            
            self.notify(f"Misc Info {'shown' if new_value else 'hidden'}", severity="information")


    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scroll-arrow":
            self.chat_log.auto_scroll = True
            self.chat_log.cursor_line = len(self.chat_log.lines) - 1
            self.query_one("#chat-log-container", Vertical).scroll_end(animate=False)
            self.chat_log.refresh()
            event.button.display = False

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        line_index = event.node.data
        if line_index is not None:
            self.chat_log.focus_line(line_index)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_autocomplete(self):
        val = self.chat_input.value
        if val.startswith("/") and self.filtered_options:
            self.chat_input.value = self.filtered_options[0]
            self.chat_input.cursor_position = len(self.filtered_options[0])

    def action_search(self):
        self.chat_input.value = "/search "
        self.chat_input.cursor_position = len("/search ")
        self.chat_input.focus()

    def action_toggle_focus(self):
        if self.chat_log.has_focus:
            self.chat_input.focus()
        else:
            self.chat_log.focus()

    def open_editor(self, filename: str):
        editor = os.environ.get('EDITOR', 'vim')
        with self.suspend():
            subprocess.run([editor, filename])
        self._post_editor()

    def _post_editor(self):
        global yaml_config
        yaml_config = ConfigManager.load_config()
        self.chat_log.write("Config reloaded successfully.")
