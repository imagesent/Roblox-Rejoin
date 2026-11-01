import concurrent.futures
import os
import queue
import sys
import threading
import time
from io import BytesIO
from math import ceil
from PIL import Image
from tkinter import messagebox

import customtkinter as ctk

import roblox_monitor
from config_manager import load_config, save_config


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

THEME_ROTATION_INTERVAL_MS = 6000
THEME_TRANSITION_DURATION_MS = 240
THEME_TRANSITION_FRAME_MS = 30
AUTO_THEME_NAME = "Chrome"
THEMES = {
    "Blue": {
        "background": "#0b1220",
        "surface": "#172338",
        "border": "#293a53",
        "input": "#101a2b",
        "console": "#0b1220",
        "accent": "#3b82f6",
        "hover": "#2563eb",
        "danger": "#ef4444",
        "danger_hover": "#b91c1c",
        "text": "#f8fafc",
        "muted": "#a7b3c7",
    },
    "Purple": {
        "background": "#130d20",
        "surface": "#211632",
        "border": "#3a2a50",
        "input": "#171025",
        "console": "#130d20",
        "accent": "#a78bfa",
        "hover": "#8b5cf6",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#faf7ff",
        "muted": "#c4b5d8",
    },
    "Emerald": {
        "background": "#071a15",
        "surface": "#10291f",
        "border": "#214737",
        "input": "#0b211a",
        "console": "#071a15",
        "accent": "#34d399",
        "hover": "#10b981",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#f0fdf4",
        "muted": "#a7cbb7",
    },
    "Sunset": {
        "background": "#1d100d",
        "surface": "#321b12",
        "border": "#573322",
        "input": "#28150f",
        "console": "#1d100d",
        "accent": "#fb923c",
        "hover": "#f97316",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#fff7ed",
        "muted": "#e7bfa5",
    },
    "Ocean": {
        "background": "#071720",
        "surface": "#102733",
        "border": "#254453",
        "input": "#0b1d27",
        "console": "#071720",
        "accent": "#38bdf8",
        "hover": "#0ea5e9",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#f0f9ff",
        "muted": "#a8c9d8",
    },
    "Rose": {
        "background": "#1d1018",
        "surface": "#301a27",
        "border": "#4b2b3d",
        "input": "#25131e",
        "console": "#1d1018",
        "accent": "#f472b6",
        "hover": "#ec4899",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#fff1f7",
        "muted": "#ddb2c5",
    },
    "Lavender": {
        "background": "#151326",
        "surface": "#24213a",
        "border": "#3c3858",
        "input": "#1b1930",
        "console": "#151326",
        "accent": "#c4b5fd",
        "hover": "#a78bfa",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#f7f5ff",
        "muted": "#c0bdd8",
    },
    "Graphite": {
        "background": "#111315",
        "surface": "#202326",
        "border": "#373c40",
        "input": "#191c1f",
        "console": "#111315",
        "accent": "#a3e635",
        "hover": "#84cc16",
        "danger": "#fb7185",
        "danger_hover": "#e11d48",
        "text": "#f4f4f5",
        "muted": "#b2b6b9",
    },
}
THEME_OPTIONS = (AUTO_THEME_NAME, *THEMES)


def _asset_path(name):
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is None:
        bundle_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(bundle_dir, "assets", name)


class RedirectStdout:
    """Send worker-thread output to a queue that Tk drains on its own thread."""

    def __init__(self, output_queue):
        self.output_queue = output_queue

    def write(self, value):
        if value:
            self.output_queue.put(value)
        return len(value)

    def flush(self):
        return None


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Roblox Auto-Rejoiner")
        self.geometry("980x800")
        self.minsize(860, 720)
        self.resizable(True, True)
        self.iconbitmap(_asset_path("app_icon.ico"))
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.config_data = load_config()
        self.monitor_thread = None
        self.monitor_running = False
        self._closing = False
        self._session_started_at = None
        self._session_elapsed_seconds = 0
        self._original_stdout = sys.stdout
        self._console_queue = queue.SimpleQueue()
        self._game_search_queue = queue.SimpleQueue()
        self._place_preview_queue = queue.SimpleQueue()
        self._game_search_generation = 0
        self._place_preview_generation = 0
        self._place_preview_after = None
        self._game_search_results = {}
        self._game_search_window = None
        self._game_search_popup_labels = []
        self._game_search_result_buttons = []
        self._game_search_preview_image = None
        configured_theme = self.config_data.get("GUI_THEME", AUTO_THEME_NAME)
        if configured_theme == "Auto Rotate":
            configured_theme = AUTO_THEME_NAME
        if configured_theme not in THEME_OPTIONS:
            configured_theme = AUTO_THEME_NAME
        self.config_data["GUI_THEME"] = configured_theme
        self._selected_theme = ctk.StringVar(value=configured_theme)
        self._theme_index = (
            0
            if configured_theme == AUTO_THEME_NAME
            else self._theme_cycle().index(configured_theme)
        )
        self._theme_rotation_after = None
        self._theme_transition_after = None
        self._theme_transition_generation = 0
        self._theme_labels = []
        self._theme_entries = []
        initial_theme = (
            THEMES["Blue"]
            if configured_theme == AUTO_THEME_NAME
            else THEMES[configured_theme]
        )
        self._active_theme = initial_theme

        self._create_widgets()
        self.var_place_id.trace_add("write", self._on_place_id_changed)
        self._apply_theme(initial_theme)
        self._apply_theme_selector(initial_theme)
        self._schedule_theme_rotation()
        self._on_place_id_changed()
        sys.stdout = RedirectStdout(self._console_queue)
        self.after(50, self._drain_console)
        self.after(1000, self._update_session_stats)

        print("Welcome to Roblox Auto-Rejoiner!")
        print("Choose a game above, then start monitoring.")

    def _create_widgets(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=32, pady=(18, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        profile_image = Image.open(_asset_path("profile.png")).convert("RGBA")
        self.profile_image = ctk.CTkImage(
            light_image=profile_image,
            dark_image=profile_image,
            size=(56, 56),
        )
        self.profile_avatar = ctk.CTkLabel(
            header,
            text="",
            image=self.profile_image,
        )
        self.profile_avatar.grid(
            row=0, column=0, rowspan=2, padx=(0, 16), sticky="w"
        )
        title_label = ctk.CTkLabel(
            header,
            text="Auto Rejoiner",
            font=ctk.CTkFont(size=30, weight="bold"),
        )
        title_label.grid(row=0, column=1, sticky="w")
        self._theme_labels.append(title_label)
        subtitle_label = ctk.CTkLabel(
            header,
            text="Keep your Roblox session connected automatically.",
            font=ctk.CTkFont(size=13),
        )
        subtitle_label.grid(row=1, column=1, pady=(3, 0), sticky="w")
        self._theme_labels.append(subtitle_label)
        self.status_frame = ctk.CTkFrame(header, corner_radius=14)
        self.status_frame.grid(row=0, column=2, rowspan=2, padx=(20, 0), sticky="e")
        status_caption = ctk.CTkLabel(
            self.status_frame,
            text="MONITOR",
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        status_caption.pack(padx=16, pady=(8, 0))
        self.lbl_mon_status = ctk.CTkLabel(
            self.status_frame,
            text="Offline",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.lbl_mon_status.pack(padx=16, pady=(0, 8))
        self._theme_labels.extend((status_caption, self.lbl_mon_status))

        self.config_frame = ctk.CTkFrame(self, corner_radius=18, border_width=0)
        self.config_frame.grid(row=1, column=0, padx=32, pady=6, sticky="ew")
        self.config_frame.grid_columnconfigure((0, 1, 2), weight=1, uniform="settings")

        settings_title = ctk.CTkLabel(
            self.config_frame,
            text="Game settings",
            font=ctk.CTkFont(size=17, weight="bold"),
        )
        settings_title.grid(
            row=0, column=0, columnspan=3, padx=22, pady=(9, 2), sticky="w"
        )
        self._theme_labels.append(settings_title)
        settings_hint = ctk.CTkLabel(
            self.config_frame,
            text="Choose what to launch and how quickly to retry.",
            font=ctk.CTkFont(size=12),
        )
        settings_hint.grid(
            row=1, column=0, columnspan=3, padx=22, pady=(0, 8), sticky="w"
        )
        self._theme_labels.append(settings_hint)

        self.var_place_id = ctk.StringVar(value=self.config_data.get("PLACE_ID", ""))
        self.var_rejoin_delay = ctk.StringVar(
            value=str(self.config_data.get("REJOIN_DELAY", 5))
        )
        fields = (
            ("PLACE ID", self.var_place_id, 0),
            ("REJOIN DELAY (SEC)", self.var_rejoin_delay, 1),
        )
        for label, variable, column in fields:
            field_label = ctk.CTkLabel(
                self.config_frame,
                text=label,
                font=ctk.CTkFont(size=10, weight="bold"),
            )
            field_label.grid(
                row=2, column=column, padx=(22, 10), pady=(0, 4), sticky="w"
            )
            self._theme_labels.append(field_label)
            entry = ctk.CTkEntry(
                self.config_frame,
                textvariable=variable,
                height=34,
                corner_radius=10,
                border_width=1,
            )
            entry.grid(
                row=3, column=column, padx=(22, 10), pady=(0, 8), sticky="ew"
            )
            self._theme_entries.append(entry)

        theme_label = ctk.CTkLabel(
            self.config_frame,
            text="COLOR THEME",
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        theme_label.grid(
            row=2, column=2, padx=(10, 22), pady=(0, 4), sticky="w"
        )
        self._theme_labels.append(theme_label)
        self.theme_menu = ctk.CTkOptionMenu(
            self.config_frame,
            variable=self._selected_theme,
            values=list(THEME_OPTIONS),
            command=self._on_theme_change,
            height=34,
            corner_radius=10,
        )
        self.theme_menu.grid(
            row=3, column=2, padx=(10, 22), pady=(0, 8), sticky="ew"
        )

        self.btn_save = ctk.CTkButton(
            self.config_frame,
            text="Save settings",
            command=self.save_cfg,
            height=34,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.btn_save.grid(
            row=6, column=0, columnspan=3, padx=22, pady=(0, 8), sticky="ew"
        )

        self.var_game_search = ctk.StringVar()
        self.game_search_entry = ctk.CTkEntry(
            self.config_frame,
            textvariable=self.var_game_search,
            placeholder_text="Search Roblox games by name",
            height=34,
            corner_radius=10,
            border_width=1,
        )
        self.game_search_entry.grid(
            row=4, column=0, columnspan=2, padx=(22, 10), pady=(0, 4), sticky="ew"
        )
        self._theme_entries.append(self.game_search_entry)
        self.btn_search_games = ctk.CTkButton(
            self.config_frame,
            text="Search games",
            command=self._start_game_search,
            height=34,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.btn_search_games.grid(
            row=4, column=2, padx=(10, 22), pady=(0, 4), sticky="ew"
        )
        self.game_search_preview_frame = ctk.CTkFrame(
            self.config_frame,
            corner_radius=10,
            height=76,
        )
        self.game_search_preview_frame.grid(
            row=5, column=0, columnspan=3, padx=22, pady=(0, 4), sticky="ew"
        )
        self.game_search_preview_frame.grid_columnconfigure(1, weight=1)
        self.game_search_icon_label = ctk.CTkLabel(
            self.game_search_preview_frame,
            text="RBX",
            width=56,
            height=56,
            corner_radius=12,
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        self.game_search_icon_label.grid(
            row=0, column=0, rowspan=2, padx=(10, 12), pady=9, sticky="w"
        )
        self.game_search_title_label = ctk.CTkLabel(
            self.game_search_preview_frame,
            text="Enter a Place ID to preview its game",
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.game_search_title_label.grid(
            row=0, column=1, padx=(0, 12), pady=(12, 1), sticky="ew"
        )
        self.game_search_detail_label = ctk.CTkLabel(
            self.game_search_preview_frame,
            text="The game name and icon load automatically.",
            anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.game_search_detail_label.grid(
            row=1, column=1, padx=(0, 12), pady=(1, 12), sticky="ew"
        )
        self._theme_labels.extend(
            (self.game_search_title_label, self.game_search_detail_label)
        )
        control_frame = ctk.CTkFrame(self, fg_color="transparent")
        control_frame.grid(row=2, column=0, padx=32, pady=(4, 8), sticky="ew")
        control_frame.grid_columnconfigure((0, 1), weight=1, uniform="actions")

        self.btn_monitor = ctk.CTkButton(
            control_frame,
            text="Start Monitor",
            command=self.toggle_monitor,
            height=46,
            corner_radius=14,
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.btn_monitor.grid(row=0, column=0, padx=(0, 7), sticky="ew")
        self.btn_join = ctk.CTkButton(
            control_frame,
            text="Join + Start Monitor",
            command=self.start_join_session,
            height=46,
            corner_radius=14,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#2FA572",
            hover_color="#106A43",
        )
        self.btn_join.grid(row=0, column=1, padx=(7, 0), sticky="ew")

        self.stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.stats_frame.grid(row=3, column=0, padx=32, pady=(0, 8), sticky="ew")
        self.stats_frame.grid_columnconfigure((0, 1), weight=1, uniform="stats")
        self.stat_time_card = ctk.CTkFrame(self.stats_frame, corner_radius=16)
        self.stat_time_card.grid(row=0, column=0, padx=(0, 7), sticky="ew")
        self.stat_rejoin_card = ctk.CTkFrame(self.stats_frame, corner_radius=16)
        self.stat_rejoin_card.grid(row=0, column=1, padx=(7, 0), sticky="ew")
        self.stat_time_label = ctk.CTkLabel(
            self.stat_time_card,
            text="SESSION TIME",
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        self.stat_time_label.pack(anchor="w", padx=18, pady=(14, 0))
        self.lbl_session_time = ctk.CTkLabel(
            self.stat_time_card,
            text="00:00:00",
            font=ctk.CTkFont(size=25, weight="bold"),
        )
        self.lbl_session_time.pack(anchor="w", padx=18, pady=(0, 14))
        self.stat_rejoin_label = ctk.CTkLabel(
            self.stat_rejoin_card,
            text="SUCCESSFUL REJOINS",
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        self.stat_rejoin_label.pack(anchor="w", padx=18, pady=(14, 0))
        self.lbl_rejoin_count = ctk.CTkLabel(
            self.stat_rejoin_card,
            text="0",
            font=ctk.CTkFont(size=25, weight="bold"),
        )
        self.lbl_rejoin_count.pack(anchor="w", padx=18, pady=(0, 14))
        self._theme_labels.extend(
            (
                self.stat_time_label,
                self.lbl_session_time,
                self.stat_rejoin_label,
                self.lbl_rejoin_count,
            )
        )

        self.console_frame = ctk.CTkFrame(self, corner_radius=18, border_width=0)
        self.console_frame.grid(
            row=4, column=0, padx=32, pady=(0, 12), sticky="nsew"
        )
        self.console_frame.grid_rowconfigure(1, weight=1)
        self.console_frame.grid_columnconfigure(0, weight=1)
        console_header = ctk.CTkFrame(self.console_frame, fg_color="transparent")
        console_header.grid(row=0, column=0, padx=20, pady=(10, 6), sticky="ew")
        console_label = ctk.CTkLabel(
            console_header,
            text="Activity",
            font=ctk.CTkFont(size=17, weight="bold"),
        )
        console_label.pack(side="left")
        console_hint = ctk.CTkLabel(
            console_header,
            text="Live monitor and reconnect messages",
            font=ctk.CTkFont(size=12),
        )
        console_hint.pack(side="left", padx=(12, 0))
        self.console = ctk.CTkTextbox(
            self.console_frame,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled",
            height=120,
            fg_color="#111827",
            text_color="#d4d4d4",
            corner_radius=12,
            border_width=0,
        )
        self.console.grid(
            row=1, column=0, padx=16, pady=(0, 10), sticky="nsew"
        )
        self._console_label = console_label
        self._theme_labels.extend((console_label, console_hint))
        self._theme_frames = (
            self.config_frame,
            self.status_frame,
            self.stat_time_card,
            self.stat_rejoin_card,
            self.console_frame,
            self.game_search_preview_frame,
        )
        self._console_hint = console_hint

    def _apply_theme(self, theme):
        self._active_theme = dict(theme)
        self.configure(fg_color=theme["background"])
        for frame in self._theme_frames:
            frame.configure(
                fg_color=theme["surface"],
                border_width=1,
                border_color=theme["border"],
            )
        for label in self._theme_labels:
            label.configure(text_color=theme["text"])
        self.lbl_mon_status.configure(
            text_color=theme["accent"] if self.monitor_running else theme["danger"]
        )
        self._console_hint.configure(text_color=theme["muted"])
        self.game_search_icon_label.configure(
            fg_color=theme["input"],
            text_color=theme["muted"],
        )
        for entry in self._theme_entries:
            entry.configure(
                fg_color=theme["input"],
                text_color=theme["text"],
                placeholder_text_color=theme["muted"],
                border_color=theme["border"],
            )
        self.console.configure(
            fg_color=theme["console"],
            text_color=theme["text"],
        )
        if self._game_search_window is not None:
            self._game_search_window.configure(fg_color=theme["background"])
            self._game_search_results_frame.configure(fg_color=theme["background"])
            for label in self._game_search_popup_labels:
                label.configure(text_color=theme["text"])
            for button in self._game_search_result_buttons:
                button.configure(
                    fg_color=theme["surface"],
                    hover_color=theme["hover"],
                    text_color=theme["text"],
                )
        self.btn_search_games.configure(
            fg_color=theme["accent"],
            hover_color=theme["hover"],
            text_color=theme["background"],
        )
        self.btn_save.configure(
            fg_color=theme["accent"],
            hover_color=theme["hover"],
            text_color=theme["background"],
        )
        self.btn_join.configure(
            fg_color=theme["accent"],
            hover_color=theme["hover"],
            text_color=theme["background"],
        )
        if self.monitor_running:
            self.btn_monitor.configure(
                fg_color=theme["danger"],
                hover_color=theme["danger_hover"],
                text_color=theme["text"],
            )
        else:
            self.btn_monitor.configure(
                fg_color=theme["accent"],
                hover_color=theme["hover"],
                text_color=theme["background"],
            )

    def _apply_theme_selector(self, theme):
        self.theme_menu.configure(
            fg_color=theme["accent"],
            button_color=theme["accent"],
            button_hover_color=theme["hover"],
            text_color=theme["text"],
            dropdown_fg_color=theme["surface"],
            dropdown_hover_color=theme["hover"],
            dropdown_text_color=theme["text"],
        )

    @staticmethod
    def _blend_theme_color(start_color, end_color, progress):
        start_rgb = tuple(
            int(start_color[index : index + 2], 16)
            for index in (1, 3, 5)
        )
        end_rgb = tuple(
            int(end_color[index : index + 2], 16)
            for index in (1, 3, 5)
        )
        blended = tuple(
            round(start + (end - start) * progress)
            for start, end in zip(start_rgb, end_rgb)
        )
        return "#{:02x}{:02x}{:02x}".format(*blended)

    def _transition_to_theme(self, target_theme):
        if self._theme_transition_after is not None:
            self.after_cancel(self._theme_transition_after)
            self._theme_transition_after = None

        self._theme_transition_generation += 1
        generation = self._theme_transition_generation
        start_theme = dict(self._active_theme)
        target_theme = dict(target_theme)
        total_steps = max(
            1,
            ceil(THEME_TRANSITION_DURATION_MS / THEME_TRANSITION_FRAME_MS),
        )
        self._apply_theme_selector(target_theme)

        def animate(step):
            if self._closing or generation != self._theme_transition_generation:
                return

            linear_progress = step / total_steps
            progress = linear_progress * linear_progress * (3 - 2 * linear_progress)
            blended_theme = {
                key: self._blend_theme_color(start_theme[key], color, progress)
                for key, color in target_theme.items()
            }
            self._apply_theme(blended_theme)

            if step < total_steps:
                self._theme_transition_after = self.after(
                    THEME_TRANSITION_FRAME_MS,
                    lambda: animate(step + 1),
                )
            else:
                self._theme_transition_after = None

        animate(1)

    def _on_theme_change(self, selection):
        previous_theme = self.config_data.get("GUI_THEME", AUTO_THEME_NAME)
        self.config_data["GUI_THEME"] = selection
        if not save_config(self.config_data):
            self.config_data["GUI_THEME"] = previous_theme
            self._selected_theme.set(previous_theme)
            messagebox.showerror("Theme error", "Could not save the selected theme.")
            return

        self._schedule_theme_rotation()
        if selection == AUTO_THEME_NAME:
            self._transition_to_theme(
                THEMES[self._theme_cycle()[self._theme_index]]
            )
        else:
            self._transition_to_theme(THEMES[selection])

    @staticmethod
    def _theme_cycle():
        return tuple(THEMES)

    def _schedule_theme_rotation(self):
        if self._theme_rotation_after is not None:
            self.after_cancel(self._theme_rotation_after)
            self._theme_rotation_after = None
        if self._selected_theme.get() == AUTO_THEME_NAME:
            self._theme_rotation_after = self.after(
                THEME_ROTATION_INTERVAL_MS,
                self._rotate_theme,
            )

    def _rotate_theme(self):
        self._theme_rotation_after = None
        if self._closing or self._selected_theme.get() != AUTO_THEME_NAME:
            return
        theme_names = self._theme_cycle()
        self._theme_index = (self._theme_index + 1) % len(theme_names)
        self._transition_to_theme(THEMES[theme_names[self._theme_index]])
        self._schedule_theme_rotation()

    def _drain_console(self):
        if self._closing:
            return
        chunks = []
        while True:
            try:
                chunks.append(self._console_queue.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self.console.configure(state="normal")
            self.console.insert("end", "".join(chunks))
            self.console.see("end")
            self.console.configure(state="disabled")
        while True:
            try:
                generation, results, error = self._game_search_queue.get_nowait()
            except queue.Empty:
                break
            if generation == self._game_search_generation:
                self._show_game_search_results(results, error)
        while True:
            try:
                generation, place_id, image_data, title, detail = (
                    self._place_preview_queue.get_nowait()
                )
            except queue.Empty:
                break
            if (
                generation == self._place_preview_generation
                and place_id == self.var_place_id.get().strip()
            ):
                self._set_game_search_preview(image_data, title, detail)
        self.after(50, self._drain_console)

    def _on_place_id_changed(self, *_):
        if self._place_preview_after is not None:
            self.after_cancel(self._place_preview_after)
            self._place_preview_after = None
        self._place_preview_generation += 1
        generation = self._place_preview_generation
        place_id = self.var_place_id.get().strip()

        if not place_id:
            self._set_game_search_preview(
                None,
                "Enter a Place ID to preview its game",
                "The game name and icon load automatically.",
            )
            return
        if not place_id.isdigit():
            self._set_game_search_preview(
                None,
                "Enter a valid Place ID",
                "Place IDs contain digits only.",
            )
            return

        self._set_game_search_preview(
            None,
            "Looking up your game...",
            f"Place ID: {place_id}",
        )
        self._place_preview_after = self.after(
            450,
            lambda: self._start_place_preview_lookup(place_id, generation),
        )

    def _start_place_preview_lookup(self, place_id, generation):
        self._place_preview_after = None
        if self._closing or generation != self._place_preview_generation:
            return
        threading.Thread(
            target=self._place_preview_worker,
            args=(place_id, generation),
            daemon=True,
        ).start()

    def _place_preview_worker(self, place_id, generation):
        universe_id = roblox_monitor.get_universe_id(place_id)
        if universe_id is None:
            self._place_preview_queue.put(
                (
                    generation,
                    place_id,
                    None,
                    "Experience unavailable",
                    f"Could not resolve Place ID {place_id}.",
                )
            )
            return

        title = roblox_monitor.get_game_name(place_id, universe_id)
        icon_url = roblox_monitor.get_game_icon_url(universe_id)
        image_data = None
        if icon_url:
            try:
                image_data = self._download_game_icon(icon_url)
            except (roblox_monitor.requests.RequestException, ValueError) as exc:
                roblox_monitor.log(f"Could not download the game icon: {exc}")

        detail = f"Place ID: {place_id}"
        if image_data is None:
            detail += "  ·  Icon unavailable"
        self._place_preview_queue.put(
            (generation, place_id, image_data, title, detail)
        )

    def _start_game_search(self):
        query = self.var_game_search.get().strip()
        if not query:
            messagebox.showinfo(
                "Search games",
                "Please enter a game name in the search box.",
            )
            self.game_search_entry.focus_set()
            return

        self._game_search_generation += 1
        generation = self._game_search_generation
        self._close_game_search_window()

        self.btn_search_games.configure(state="disabled", text="Searching...")
        self._set_game_search_preview(
            None,
            "Searching Roblox...",
            "Fetching game results and icons.",
        )
        threading.Thread(
            target=self._search_games_worker,
            args=(query, generation),
            daemon=True,
        ).start()

    def _search_games_worker(self, query, generation):
        try:
            results = roblox_monitor.search_games(query, limit=12)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                icon_futures = {
                    executor.submit(self._download_game_icon, game["icon_url"]): game
                    for game in results
                    if game.get("icon_url")
                }
                for future in concurrent.futures.as_completed(icon_futures):
                    game = icon_futures[future]
                    try:
                        game["icon_data"] = future.result()
                    except (roblox_monitor.requests.RequestException, ValueError) as exc:
                        roblox_monitor.log(
                            f"Could not download the icon for {game['name']}: {exc}"
                        )
                        game["icon_data"] = None
            error = None
        except (roblox_monitor.requests.RequestException, ValueError) as exc:
            results = []
            error = str(exc) or "The Roblox game search failed."
        self._game_search_queue.put((generation, results, error))

    @staticmethod
    def _download_game_icon(icon_url):
        response = roblox_monitor.requests.get(icon_url, timeout=6)
        response.raise_for_status()
        if not response.content:
            raise ValueError("Roblox returned an empty game icon.")
        return response.content

    def _show_game_search_results(self, results, error):
        self.btn_search_games.configure(state="normal", text="Search games")
        self._game_search_results = {}
        if error:
            self._set_game_search_preview(
                None,
                "Game search failed",
                error,
            )
            return
        if not results:
            self._set_game_search_preview(
                None,
                "No games found",
                "Try another search phrase.",
            )
            return

        self._game_search_results = {
            game["place_id"]: game
            for game in results
        }
        self._open_game_search_window(results)

    def _open_game_search_window(self, results):
        if self._game_search_window is not None:
            self._game_search_window.destroy()
        theme = self._active_theme
        window = ctk.CTkToplevel(self)
        self._game_search_window = window
        self._game_search_popup_labels = []
        self._game_search_result_buttons = []
        window.title("Choose a Roblox game")
        window.iconbitmap(_asset_path("app_icon.ico"))
        window.geometry("600x540")
        window.resizable(False, False)
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", self._close_game_search_window)
        window.configure(fg_color=theme["background"])
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        heading = ctk.CTkLabel(
            window,
            text="Choose a game",
            anchor="w",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color=theme["text"],
        )
        heading.grid(row=0, column=0, padx=20, pady=(18, 2), sticky="ew")
        hint = ctk.CTkLabel(
            window,
            text="Select a result to set its Place ID and show its icon.",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=theme["muted"],
        )
        hint.grid(row=1, column=0, padx=20, pady=(0, 12), sticky="ew")
        results_frame = ctk.CTkScrollableFrame(
            window,
            fg_color=theme["background"],
            scrollbar_button_color=theme["accent"],
            scrollbar_button_hover_color=theme["hover"],
        )
        results_frame.grid(row=2, column=0, padx=14, pady=(0, 16), sticky="nsew")
        self._game_search_results_frame = results_frame

        self._game_search_popup_labels.extend((heading, hint))
        for game in results:
            icon = self._make_game_icon(game.get("icon_data"), (48, 48))
            label = (
                f"{game['name']}\n"
                f"{game['creator']}  ·  {game['player_count']:,} playing"
            )
            result_button = ctk.CTkButton(
                results_frame,
                text=label,
                image=icon,
                compound="left",
                anchor="w",
                height=68,
                corner_radius=10,
                fg_color=theme["surface"],
                hover_color=theme["hover"],
                text_color=theme["text"],
                font=ctk.CTkFont(size=12),
                command=lambda selected=game: self._select_game_search_result(
                    selected
                ),
            )
            result_button.pack(fill="x", padx=4, pady=4)
            self._game_search_result_buttons.append(result_button)
        window.focus()

    @staticmethod
    def _make_game_icon(image_data, size):
        if not image_data:
            return None
        try:
            with Image.open(BytesIO(image_data)) as image:
                icon = image.convert("RGBA")
                icon.thumbnail(size, Image.Resampling.LANCZOS)
                return ctk.CTkImage(
                    light_image=icon,
                    dark_image=icon,
                    size=size,
                )
        except (OSError, ValueError) as exc:
            roblox_monitor.log(f"Could not display a game icon: {exc}")
            return None

    def _select_game_search_result(self, game):
        self.var_place_id.set(game["place_id"])
        if self._place_preview_after is not None:
            self.after_cancel(self._place_preview_after)
            self._place_preview_after = None
        self._place_preview_generation += 1
        self._set_game_search_preview(
            game.get("icon_data"),
            game["name"],
            f"{game['creator']}  ·  {game['player_count']:,} playing  ·  "
            f"Place {game['place_id']}",
        )
        self._close_game_search_window()

    def _set_game_search_preview(self, image_data, title, detail):
        self._game_search_preview_image = self._make_game_icon(
            image_data,
            (56, 56),
        )
        if self._game_search_preview_image is None:
            self.game_search_icon_label.configure(image="", text="RBX")
        else:
            self.game_search_icon_label.configure(
                image=self._game_search_preview_image,
                text="",
            )
        self.game_search_title_label.configure(text=title)
        self.game_search_detail_label.configure(text=detail)

    def _close_game_search_window(self):
        if self._game_search_window is not None:
            self._game_search_window.destroy()
            self._game_search_window = None
            self._game_search_popup_labels = []
            self._game_search_result_buttons = []

    def save_cfg(self, show_success=True):
        try:
            place_id = self.var_place_id.get().strip()
            if not place_id.isdigit():
                raise ValueError("Place ID must contain digits only")
            rejoin_delay = float(self.var_rejoin_delay.get())
            if rejoin_delay < 0:
                raise ValueError("Rejoin delay cannot be negative")
            self.config_data.update(
                PLACE_ID=place_id,
                REJOIN_DELAY=rejoin_delay,
            )
            if not save_config(self.config_data):
                raise OSError("Could not write config.json")
            roblox_monitor.reload_monitor_config()
            print("Configuration saved.")
            if show_success:
                messagebox.showinfo("Success", "Configuration saved.")
            return True
        except (ValueError, OSError) as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return False

    @staticmethod
    def _format_duration(seconds):
        hours, remainder = divmod(max(0, seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _update_session_stats(self):
        if self._closing:
            return
        with roblox_monitor.state_lock:
            session_started = roblox_monitor.state["session_started_monotonic"]
            rejoin_count = roblox_monitor.state["rejoin_count"]
            saved_elapsed = roblox_monitor.state["session_elapsed_seconds"]
        if session_started is not None:
            self._session_elapsed_seconds = int(time.monotonic() - session_started)
        else:
            self._session_elapsed_seconds = max(
                self._session_elapsed_seconds,
                saved_elapsed,
            )
        self.lbl_session_time.configure(
            text=self._format_duration(self._session_elapsed_seconds)
        )
        self.lbl_rejoin_count.configure(
            text=str(rejoin_count)
        )
        self.after(1000, self._update_session_stats)

    def toggle_monitor(self):
        if not self.monitor_running:
            self._start_monitor()
        else:
            self._stop_monitor()

    def start_join_session(self):
        if self.monitor_running:
            return
        if not self.save_cfg(show_success=False):
            return
        self._start_monitor(initial_join=True)

    def _start_monitor(self, initial_join=False):
        place_id = self.var_place_id.get().strip()
        if not place_id.isdigit():
            messagebox.showerror("Invalid configuration", "Save a valid Place ID first.")
            return

        self.monitor_running = True
        self._session_started_at = time.monotonic()
        self._session_elapsed_seconds = 0
        roblox_monitor.MONITOR_ACTIVE = True
        with roblox_monitor.state_lock:
            roblox_monitor.state["monitoring"] = True
            roblox_monitor.state["rejoin_count"] = 0
            roblox_monitor.state["session_started_monotonic"] = self._session_started_at
            roblox_monitor.state["session_elapsed_seconds"] = 0

        self.btn_monitor.configure(
            text="Stop Monitor",
            fg_color=self._active_theme["danger"],
            hover_color=self._active_theme["danger_hover"],
        )
        self.btn_join.configure(state="disabled")
        self.lbl_mon_status.configure(
            text="Active",
            text_color=self._active_theme["accent"],
        )

        self.monitor_thread = threading.Thread(
            target=self._run_monitor,
            args=(initial_join,),
            daemon=True,
        )
        self.monitor_thread.start()

    def _run_monitor(self, initial_join=False):
        try:
            roblox_monitor.monitor_loop(initial_join=initial_join)
        except Exception as exc:
            print(f"Monitor error: {exc}")
        finally:
            if not self._closing:
                self.after(0, self._set_monitor_stopped)

    def _stop_monitor(self):
        roblox_monitor.MONITOR_ACTIVE = False
        self.lbl_mon_status.configure(
            text="Stopping",
            text_color=self._active_theme["accent"],
        )
        self.btn_monitor.configure(state="disabled")
        self.btn_join.configure(state="disabled")
        print("Stopping monitor services...")

    def _set_monitor_stopped(self):
        self.monitor_running = False
        if self._session_started_at is not None:
            self._session_elapsed_seconds = max(
                self._session_elapsed_seconds,
                int(time.monotonic() - self._session_started_at),
            )
            self._session_started_at = None
        with roblox_monitor.state_lock:
            roblox_monitor.state["session_elapsed_seconds"] = (
                self._session_elapsed_seconds
            )
            roblox_monitor.state["session_started_monotonic"] = None
        self.btn_monitor.configure(
            text="Start Monitor",
            fg_color=self._active_theme["accent"],
            hover_color=self._active_theme["hover"],
        )
        self.btn_join.configure(state="normal")
        self.lbl_mon_status.configure(
            text="Offline",
            text_color=self._active_theme["danger"],
        )
        self.lbl_session_time.configure(
            text=self._format_duration(self._session_elapsed_seconds)
        )

    def close_app(self):
        self._closing = True
        if self._theme_rotation_after is not None:
            self.after_cancel(self._theme_rotation_after)
        if self._theme_transition_after is not None:
            self.after_cancel(self._theme_transition_after)
        if self._place_preview_after is not None:
            self.after_cancel(self._place_preview_after)
        roblox_monitor.MONITOR_ACTIVE = False
        sys.stdout = self._original_stdout
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
