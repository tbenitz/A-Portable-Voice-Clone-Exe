"""Desktop UI: Pocket TTS voice studio for Windows.

The hotkey triggers microphone capture; speech-to-speech is on-device ASR -> TTS
after a recording ends, not low-latency simultaneous voice conversion.
"""
from __future__ import annotations

import argparse
import copy
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from .core import (
    AUDIO_EXTENSIONS, TEXT_EXTENSIONS, DEFAULT_SETTINGS, PRESETS, Library, PocketEngine, blend_audio,
    data_home, export_audio, parameters, transcribe_local,
)

LOG = logging.getLogger("voice-studio")


def setup_log(root):
    location = root / "last-error.txt"
    logging.basicConfig(filename=location, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    return location


def cli_main(args):
    library = Library()
    setup_log(library.root)
    if args.self_test:
        import imageio_ffmpeg
        import pocket_tts_onnx
        import faster_whisper
        info = {"imports": ["pocket_tts_onnx", "faster_whisper", "imageio_ffmpeg"],
                "ffmpeg": str(imageio_ffmpeg.get_ffmpeg_exe()),
                "data_home": str(library.root)}
        (library.root / "runtime-self-test.json").write_text(__import__("json").dumps(info, indent=2), encoding="utf-8")
        return 0
    if args.voice_id is None:
        raise ValueError("--voice-id is required for command-line generation.")
    voice = library.get_voice(args.voice_id)
    if not voice:
        raise ValueError("Unknown voice ID; use the UI to import a reference.")
    text = args.speak_text or (Path(args.speak_file).read_text(encoding="utf-8-sig") if args.speak_file else "")
    if not text.strip():
        raise ValueError("No text provided.")
    engine = PocketEngine(library)
    sound, rate = engine.synthesize(text, voice, library.settings, status=print)
    if not len(sound):
        raise RuntimeError("No audio generated.")
    destination = Path(args.output or (library.root / "exports" / "cli.wav"))
    export_audio(sound, rate, destination)
    library.append_history(text, voice["id"], sound, rate)
    print("Saved", destination)
    return 0


class Studio:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.lib = Library()
        self.logfile = setup_log(self.lib.root)
        self.engine = PocketEngine(self.lib)
        self.root = tk.Tk()
        self.root.title("Voice Clone Studio · Pocket TTS")
        self.root.geometry("1160x785")
        self.root.minsize(840, 585)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-Return>", lambda e: self.generate())
        self.root.bind("<Control-s>", lambda e: self.export_latest())
        self.root.bind("<Escape>", lambda e: self.stop())
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.active_stream = None
        self.record_stream = None
        self.record_frames = []
        self.record_rate = 16000
        self.record_mode = None
        self.record_lock = threading.Lock()
        self.last_audio = None
        self.last_rate = 24000
        self.last_text = ""
        self.last_voice_id = None
        self.compare_files = [None, None]
        self.selected_voice = None
        self.menu_values = {}
        self.tray = None
        self.listener = None
        self.hotkey_active = False
        self.floating = None
        self.collapsed = False
        self.voice_name = tk.StringVar(value="No voice selected")
        self.current_label = tk.StringVar(value="Ready · Pocket TTS ONNX · local audio")
        self.preset = tk.StringVar(value=self.lib.settings.get("preset", "Balanced"))
        self.speed = tk.DoubleVar(value=self.lib.settings.get("speed", 1.0))
        self.pitch = tk.DoubleVar(value=self.lib.settings.get("pitch", 0.0))
        self.stability = tk.DoubleVar(value=self.lib.settings.get("stability", 50))
        self.clarity = tk.DoubleVar(value=self.lib.settings.get("clarity", 0))
        self.consent = tk.BooleanVar(value=False)
        self.filter_text = tk.StringVar(value="")
        self.search_history = tk.StringVar(value="")
        self.hotkey_var = tk.StringVar(value=self.lib.settings.get("hotkey", "<ctrl>+<alt>+v"))
        self.hotkey_mode_var = tk.StringVar(value=self.lib.settings.get("hotkey_mode", "toggle"))
        self.theme_var = tk.StringVar(value=self.lib.settings.get("theme", "dark"))
        self.output_var = tk.StringVar(value=self.lib.settings.get("output_device", ""))
        self.sample_rate_var = tk.StringVar(value=str(self.lib.settings.get("sample_rate", 24000)))
        self.channel_var = tk.StringVar(value=str(self.lib.settings.get("channels", 1)))
        self.bitrate_var = tk.StringVar(value=str(self.lib.settings.get("mp3_bitrate", 192)))
        self.history_limit_var = tk.StringVar(value=str(self.lib.settings.get("history_limit", 50)))
        self.safe_var = tk.BooleanVar(value=self.lib.settings.get("safe_mode", True))
        self.tray_var = tk.BooleanVar(value=self.lib.settings.get("minimize_to_tray", False))
        self.mix_var = tk.StringVar(value="")
        self.mix_percent = tk.DoubleVar(value=self.lib.settings.get("mix_percent", 0))
        self._style()
        self.build_ui()
        self.refresh_voices()
        self.refresh_history()
        self.start_hotkey()
        self.start_tray()
        self.root.after(120, self.poll)

    def _style(self):
        ttk = self.ttk
        theme = self.theme_var.get()
        if theme == "light":
            self.bg, self.fg, self.card, self.accent = "#f4f8fd", "#1b2634", "#e6edf5", "#157da8"
        elif theme == "high contrast":
            self.bg, self.fg, self.card, self.accent = "#050505", "#ffffff", "#222222", "#ffe400"
        else:
            self.bg, self.fg, self.card, self.accent = "#101a2a", "#e9f3ff", "#1e3046", "#4dd5f1"
        self.root.configure(bg=self.bg)
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure(".", background=self.bg, foreground=self.fg, font=("Segoe UI", 10))
        self.style.configure("TFrame", background=self.bg)
        self.style.configure("Card.TFrame", background=self.card)
        self.style.configure("TLabel", background=self.bg, foreground=self.fg)
        self.style.configure("Title.TLabel", font=("Segoe UI Semibold", 19), foreground=self.accent)
        self.style.configure("Sub.TLabel", foreground=self.accent, font=("Segoe UI Semibold", 10))
        self.style.configure("TButton", background=self.card, foreground=self.fg, padding=(12, 9))
        self.style.map("TButton", background=[("active", self.accent)])
        self.style.configure("Primary.TButton", background=self.accent, foreground="#07131a",
                             font=("Segoe UI Semibold", 11), padding=(15, 11))
        self.style.configure("TNotebook", background=self.bg, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=self.card, foreground=self.fg,
                             padding=(16, 12), font=("Segoe UI Semibold", 10))
        self.style.map("TNotebook.Tab", background=[("selected", self.accent)], foreground=[("selected", "#09131c")])
        self.style.configure("Treeview", background=self.card, foreground=self.fg,
                             fieldbackground=self.card, rowheight=29, borderwidth=0)
        self.style.configure("Treeview.Heading", background=self.bg, foreground=self.accent)
        self.style.map("Treeview", background=[("selected", "#126683")])

    def frame(self, parent, **kwargs):
        return self.ttk.Frame(parent, **kwargs)

    def label(self, parent, text, **kwargs):
        return self.ttk.Label(parent, text=text, **kwargs)

    def make_btn(self, parent, title, cb, primary=False):
        return self.ttk.Button(parent, text=title, command=cb,
                               style="Primary.TButton" if primary else "TButton")

    def build_ui(self):
        tk, ttk = self.tk, self.ttk
        header = self.frame(self.root, padding=(22, 12, 22, 10))
        header.pack(fill="x")
        self.label(header, "VOICE CLONE STUDIO", style="Title.TLabel").pack(side="left")
        self.label(header, "Pocket TTS · all processing on your PC", style="Sub.TLabel").pack(side="left", padx=25)
        self.make_btn(header, "▣ Mini panel", self.show_floating).pack(side="right", padx=4)
        self.make_btn(header, "■ Stop", self.stop).pack(side="right", padx=4)
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.studio = self.frame(self.tabs, padding=16)
        self.library_tab = self.frame(self.tabs, padding=16)
        self.batch_tab = self.frame(self.tabs, padding=16)
        self.history_tab = self.frame(self.tabs, padding=16)
        self.settings_tab = self.frame(self.tabs, padding=16)
        for tab, title in ((self.studio, "Studio"), (self.library_tab, "Voice library"),
                           (self.batch_tab, "Batch"), (self.history_tab, "History / A-B"),
                           (self.settings_tab, "Settings")):
            self.tabs.add(tab, text=title)
        self.make_studio()
        self.make_library()
        self.make_batch()
        self.make_history()
        self.make_settings()
        footer = self.frame(self.root, padding=(22, 0, 22, 12))
        footer.pack(fill="x")
        self.ttk.Progressbar(footer, mode="indeterminate", length=140).pack(side="right")
        self.label(footer, "").pack(side="left")
        self.status_widget = self.label(footer, "", textvariable=self.current_label)
        self.status_widget.pack(side="left")
        self.progress = footer.winfo_children()[0] if False else None
        self.progressbar = next((c for c in footer.winfo_children() if isinstance(c, self.ttk.Progressbar)), None)

    def text_area(self, parent, height=15):
        container = self.frame(parent)
        text = self.tk.Text(container, height=height, wrap="word", undo=True, relief="flat",
                            bg=self.card, fg=self.fg, insertbackground=self.fg,
                            selectbackground="#246b86", font=("Segoe UI", 11), padx=15, pady=15)
        bar = self.ttk.Scrollbar(container, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return container, text

    def make_studio(self):
        ttk = self.ttk
        top = self.frame(self.studio)
        top.pack(fill="x", pady=(0, 14))
        self.label(top, "ACTIVE VOICE", style="Sub.TLabel").pack(anchor="w")
        self.voice_combo = ttk.Combobox(top, state="readonly", width=45)
        self.voice_combo.pack(side="left", padx=(0, 9), pady=5)
        self.voice_combo.bind("<<ComboboxSelected>>", self.voice_changed)
        self.make_btn(top, "Import / record voice", lambda: self.tabs.select(self.library_tab)).pack(side="left")
        self.label(self.studio, "TEXT TO SPEAK", style="Sub.TLabel").pack(anchor="w")
        txtframe, self.text = self.text_area(self.studio, height=12)
        txtframe.pack(fill="both", expand=True, pady=(5, 12))
        self.text.insert("1.0", "Type or paste text here, choose a voice, then Generate.\n\nCtrl+Enter starts generation. Ctrl+S exports the latest audio.")
        row = self.frame(self.studio)
        row.pack(fill="x", pady=(0, 10))
        self.make_btn(row, "▶ Generate & listen", self.generate, primary=True).pack(side="left", padx=(0, 8))
        self.make_btn(row, "↻ Replay last", self.replay_latest).pack(side="left", padx=4)
        self.make_btn(row, "Save WAV/MP3", self.export_latest).pack(side="left", padx=4)
        self.make_btn(row, "A / B comparison", self.compare).pack(side="left", padx=4)
        self.make_btn(row, "Paste clipboard", self.paste_clipboard).pack(side="left", padx=4)
        self.make_btn(row, "Mic → cloned voice", lambda: self.start_record("speech")).pack(side="right")
        ctrls = self.frame(self.studio)
        ctrls.pack(fill="x")
        presetrow = self.frame(ctrls)
        presetrow.pack(fill="x", pady=6)
        self.label(presetrow, "Quality", style="Sub.TLabel").pack(side="left")
        ttk.Combobox(presetrow, textvariable=self.preset, state="readonly",
                     values=list(PRESETS), width=18).pack(side="left", padx=12)
        self.label(presetrow, "Longer processing isn't guaranteed to sound better.").pack(side="left")
        slider_grid = self.frame(ctrls)
        slider_grid.pack(fill="x")
        for i, (title, variable, lo, hi, steps) in enumerate((
            ("Speed ×", self.speed, .5, 2, .05), ("Pitch (semitones)", self.pitch, -12, 12, 1),
            ("Stability", self.stability, 0, 100, 5), ("Clarity / denoise", self.clarity, 0, 100, 5),
        )):
            frame = self.frame(slider_grid)
            frame.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 16), pady=7)
            self.label(frame, title).pack(side="left")
            self.tk.Scale(frame, variable=variable, from_=lo, to=hi,
                          resolution=steps, orient="horizontal", showvalue=True,
                          length=230, bg=self.bg, fg=self.fg,
                          highlightthickness=0, troughcolor=self.card,
                          command=lambda _: self.update_floating()).pack(side="right")
        slider_grid.columnconfigure(0, weight=1)
        slider_grid.columnconfigure(1, weight=1)
        mix = self.frame(ctrls)
        mix.pack(fill="x", pady=(8, 0))
        self.label(mix, "Experimental voice blend").pack(side="left")
        self.mix_combo = ttk.Combobox(mix, textvariable=self.mix_var, state="readonly", width=24)
        self.mix_combo.pack(side="left", padx=10)
        self.tk.Scale(mix, variable=self.mix_percent, from_=0, to=100, orient="horizontal",
                      showvalue=True, length=145, bg=self.bg, fg=self.fg, highlightthickness=0).pack(side="left")
        self.label(mix, "0 = off · 100 = second voice").pack(side="left", padx=10)

    def make_library(self):
        ttk = self.ttk
        row = self.frame(self.library_tab)
        row.pack(fill="x", pady=(0, 9))
        self.label(row, "SEARCH VOICES & TAGS", style="Sub.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.filter_text, width=27).pack(side="left", padx=12)
        self.filter_text.trace_add("write", lambda *_: self.refresh_voices())
        self.make_btn(row, "Import reference file", self.import_reference).pack(side="right", padx=4)
        self.make_btn(row, "● Record a reference", lambda: self.start_record("reference")).pack(side="right", padx=4)
        self.lib_tree = ttk.Treeview(self.library_tab, columns=("name", "health", "length", "tags"),
                                    show="headings", height=12, selectmode="browse")
        for key, title, width in (("name", "Name", 260), ("health", "Voice health", 120),
                                  ("length", "Reference length", 130), ("tags", "Tags", 290)):
            self.lib_tree.heading(key, text=title)
            self.lib_tree.column(key, width=width, stretch=True)
        self.lib_tree.pack(fill="both", expand=True)
        self.lib_tree.bind("<<TreeviewSelect>>", self.library_changed)
        controls = self.frame(self.library_tab)
        controls.pack(fill="x", pady=(10, 4))
        exports = self.frame(self.library_tab)
        exports.pack(fill="x", pady=(0, 10))
        actions = (
            ("Use in Studio", self.use_selected), ("▶ Preview", self.preview_voice),
            ("★ Favorite", self.favorite), ("Duplicate", self.duplicate_voice),
            ("Edit details", self.edit_voice), ("Export .voicepack", self.export_voice),
            ("Import .voicepack", self.import_pack),
            ("Single-voice EXE", self.export_personal_exe),
            ("Delete", self.delete_voice),
        )
        for index, (title, cb) in enumerate(actions):
            parent = controls if index < 5 else exports
            self.make_btn(parent, title, cb).pack(side="left", padx=3, pady=3)
        self.health_details = self.label(self.library_tab, "Import a voice to inspect signal health.")
        self.health_details.pack(fill="x", pady=9)
        self.label(self.library_tab,
                   "Long references are scanned for the cleanest 18s excerpt. The model does not train on the full recording.").pack(anchor="w")
        ttk.Checkbutton(self.library_tab, text="I own or have permission to clone each imported voice",
                        variable=self.consent).pack(anchor="w", pady=10)

    def make_batch(self):
        ttk = self.ttk
        self.label(self.batch_tab, "BATCH VOICE OUTPUT", style="Title.TLabel").pack(anchor="w", pady=12)
        self.label(self.batch_tab, "Process text files or audio recordings using the selected Studio voice. "
                   "Audio is transcribed locally before it is resynthesized.").pack(anchor="w", pady=9)
        row = self.frame(self.batch_tab)
        row.pack(fill="x", pady=14)
        self.make_btn(row, "Choose files or folder", self.select_batch).pack(side="left", padx=5)
        self.make_btn(row, "■ Cancel batch", self.stop).pack(side="left", padx=5)
        self.batch_files = []
        self.batch_label = self.label(self.batch_tab, "No items selected")
        self.batch_label.pack(anchor="w")
        self.batch_tree = ttk.Treeview(self.batch_tab, columns=("file", "status"), show="headings", height=12)
        self.batch_tree.heading("file", text="File")
        self.batch_tree.heading("status", text="Status")
        self.batch_tree.column("file", width=620)
        self.batch_tree.column("status", width=240)
        self.batch_tree.pack(fill="both", expand=True, pady=20)
        self.make_btn(self.batch_tab, "▶ Run batch & save to exports/batch", self.run_batch, primary=True).pack(anchor="w")
        self.label(self.batch_tab, "Supports .txt/.md and common audio formats; ASR needs optional faster-whisper models.").pack(anchor="w", pady=12)

    def make_history(self):
        ttk = self.ttk
        self.label(self.history_tab, "RECENT GENERATIONS", style="Title.TLabel").pack(anchor="w", pady=6)
        self.history_tree = ttk.Treeview(self.history_tab, columns=("when", "voice", "text"),
                                         show="headings", height=13)
        for key, title, width in (("when", "Time", 160), ("voice", "Voice", 220), ("text", "Text preview", 600)):
            self.history_tree.heading(key, text=title)
            self.history_tree.column(key, width=width, stretch=True)
        self.history_tree.pack(fill="both", expand=True, pady=14)
        row = self.frame(self.history_tab)
        row.pack(fill="x", pady=7)
        self.make_btn(row, "▶ Replay", self.replay_history).pack(side="left", padx=5)
        self.make_btn(row, "Export selected", self.export_history).pack(side="left", padx=5)
        self.make_btn(row, "Reuse text/settings", self.reuse_history).pack(side="left", padx=5)
        self.label(self.history_tab, "A/B compares the same text and voice at Balanced vs Ultra. "
                   "Results are kept as WAV files in local history.").pack(anchor="w", pady=11)
        row2 = self.frame(self.history_tab)
        row2.pack(fill="x")
        self.make_btn(row2, "▶ Play A", lambda: self.play_compare(0)).pack(side="left", padx=5)
        self.make_btn(row2, "▶ Play B", lambda: self.play_compare(1)).pack(side="left", padx=5)

    def make_settings(self):
        ttk = self.ttk
        container = self.frame(self.settings_tab)
        container.pack(fill="both", expand=True)
        self.label(container, "DESKTOP CONTROLS", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(5, 16))
        fields = [
            ("Global mic hotkey", ttk.Entry(container, textvariable=self.hotkey_var, width=25)),
            ("Hotkey behavior", ttk.Combobox(container, textvariable=self.hotkey_mode_var, state="readonly", values=["toggle", "hold"], width=21)),
            ("Color theme", ttk.Combobox(container, textvariable=self.theme_var, state="readonly", values=["dark", "light", "high contrast"], width=21)),
            ("Output device", ttk.Combobox(container, textvariable=self.output_var, values=self.output_devices(), width=48)),
            ("Output sample rate", ttk.Combobox(container, textvariable=self.sample_rate_var, state="readonly", values=["16000", "22050", "24000", "44100", "48000"], width=21)),
            ("Output channels", ttk.Combobox(container, textvariable=self.channel_var, state="readonly", values=["1", "2"], width=21)),
            ("MP3 bitrate (kbps)", ttk.Combobox(container, textvariable=self.bitrate_var, state="readonly", values=["96", "128", "192", "256", "320"], width=21)),
        ]
        for idx, (name, widget) in enumerate(fields, 1):
            self.label(container, name).grid(row=idx, column=0, sticky="w", pady=10, padx=(0, 20))
            widget.grid(row=idx, column=1, sticky="w", pady=10)
        ttk.Checkbutton(container, text="Safe mode: limit pitch drift to ±8 semitones",
                        variable=self.safe_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Checkbutton(container, text="Minimize to system tray instead of exiting",
                        variable=self.tray_var).grid(row=8, column=2, sticky="w", padx=20)
        row = self.frame(container)
        row.grid(row=9, column=0, columnspan=2, sticky="w", pady=20)
        self.make_btn(row, "Save settings + rebind hotkey", self.save_settings, primary=True).pack(side="left", padx=4)
        self.make_btn(row, "Pronunciation dictionary", self.edit_dictionary).pack(side="left", padx=4)
        self.make_btn(row, "Save working profile", self.save_profile).pack(side="left", padx=4)
        self.make_btn(row, "Load working profile", self.load_profile).pack(side="left", padx=4)
        self.label(container, f"Local data and model cache: {self.lib.root}").grid(row=10, column=0, columnspan=3, sticky="w", pady=10)
        self.label(container, "First-run model downloads require internet; once cached, synthesis runs offline. "
                   "For OBS or Discord, choose an installed virtual audio cable as the output device.").grid(row=11, column=0, columnspan=3, sticky="w", pady=9)
        self.label(container, "Hotkey syntax example: <ctrl>+<alt>+v   (using pynput). "
                   "Mic capture → Whisper transcription → Pocket synthesis on release.").grid(row=12, column=0, columnspan=3, sticky="w", pady=9)
        self.label(container, "History entries to retain (1–500)").grid(row=13, column=0, sticky="w", pady=9)
        ttk.Combobox(container, textvariable=self.history_limit_var, state="readonly",
                     values=["10", "25", "50", "100", "200", "500"], width=18).grid(row=13, column=1, sticky="w")

    def output_devices(self):
        try:
            return [""] + [str(i) + ": " + str(item["name"]) for i, item in enumerate(sd.query_devices()) if item["max_output_channels"]]
        except Exception:
            return [""]

    def output_device_id(self):
        name = self.output_var.get()
        if re.match(r"^\d+:", name):
            return int(name.split(":", 1)[0])
        return None

    def save_settings(self):
        self.collect_settings()
        self.lib.save()
        self.start_hotkey()
        self._style()
        self.set_status("Settings saved. Selected output device is used for playback.")

    def collect_settings(self):
        self.lib.settings.update({
            "preset": self.preset.get(), "speed": float(self.speed.get()), "pitch": float(self.pitch.get()),
            "stability": float(self.stability.get()), "clarity": float(self.clarity.get()),
            "hotkey": self.hotkey_var.get().strip(), "hotkey_mode": self.hotkey_mode_var.get(),
            "theme": self.theme_var.get(), "output_device": self.output_var.get(),
            "sample_rate": int(self.sample_rate_var.get()), "channels": int(self.channel_var.get()),
            "mp3_bitrate": int(self.bitrate_var.get()), "safe_mode": bool(self.safe_var.get()),
            "minimize_to_tray": bool(self.tray_var.get()),
            "mix_percent": float(self.mix_percent.get()),
            "history_limit": int(self.history_limit_var.get()),
        })
        self.lib.save()

    def selected_id(self):
        return self.menu_values.get(self.voice_combo.get(), self.last_voice_id or "")

    def refresh_voices(self):
        term = self.filter_text.get().lower().strip()
        visible = [v for v in self.lib.voices if term in (v["name"] + " " + v.get("tags", "") + " " + v.get("notes", "")).lower()]
        visible.sort(key=lambda v: (not v.get("favorite", False), v["name"].lower()))
        if hasattr(self, "lib_tree"):
            for key in self.lib_tree.get_children():
                self.lib_tree.delete(key)
            for voice in visible:
                health = voice.get("health", {})
                self.lib_tree.insert("", "end", iid=voice["id"], values=(
                    ("★ " if voice.get("favorite") else "") + voice["name"],
                    str(health.get("score", "?")) + "/100", f'{voice.get("source_length",0):.1f}s',
                    voice.get("tags", "")))
        self.menu_values = {("★ " if v.get("favorite") else "") + v["name"] + f" [{v['id'][:6]}]": v["id"] for v in self.lib.voices}
        self.voice_combo.configure(values=list(self.menu_values))
        if self.selected_voice and self.lib.get_voice(self.selected_voice):
            display = next((title for title, id in self.menu_values.items() if id == self.selected_voice), "")
            self.voice_combo.set(display)
        elif self.lib.voices:
            self.selected_voice = self.lib.voices[0]["id"]
            display = next(iter(self.menu_values))
            self.voice_combo.set(display)
        else:
            self.selected_voice = None
            self.voice_combo.set("")
        self.mix_combo.configure(values=[""] + list(self.menu_values))
        self.update_floating()

    def voice_changed(self, *_):
        self.selected_voice = self.selected_id()
        voice = self.lib.get_voice(self.selected_voice)
        if voice:
            self.voice_name.set(voice["name"])
        self.update_floating()

    def library_changed(self, *_):
        selected = self.lib_tree.selection()
        if selected:
            voice = self.lib.get_voice(selected[0])
            if voice:
                m = voice.get("health", {})
                self.health_details.configure(text=f'Voice health: {m.get("score","?")}/100 | '
                    f'Selected {m.get("selected_length","?")}s from {m.get("duration_seconds","?")}s | '
                    f'Peak {m.get("peak_db","?")} dBFS | {m.get("hint","")}')
                self.selected_voice = voice["id"]

    def selected_library_voice(self):
        selection = self.lib_tree.selection()
        return self.lib.get_voice(selection[0]) if selection else self.lib.get_voice(self.selected_voice)

    def use_selected(self):
        voice = self.selected_library_voice()
        if voice:
            self.selected_voice = voice["id"]
            self.refresh_voices()
            self.tabs.select(self.studio)

    def import_reference(self):
        from tkinter import filedialog, simpledialog, messagebox
        if not self.consent.get():
            messagebox.showwarning("Permission required", "Check the permission confirmation before importing.")
            return
        file = filedialog.askopenfilename(title="Select speech reference",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.aiff"), ("All files", "*.*")])
        if file:
            name = simpledialog.askstring("Voice name", "Name this voice:", initialvalue=Path(file).stem, parent=self.root)
            if name is None:
                return
            self.run_async("Analyzing and importing reference", lambda: self._add_voice(file, name))

    def _add_voice(self, file, name):
        voice = self.lib.add_voice(file, name, clarity=self.clarity.get(), consent=True)
        self.emit("refresh", voice["id"])
        return voice

    def import_pack(self):
        from tkinter import filedialog, messagebox
        if not self.consent.get():
            messagebox.showwarning("Permission required", "Confirm voice permission first.")
            return
        file = filedialog.askopenfilename(filetypes=[("Voice pack", "*.voicepack"), ("ZIP", "*.zip")])
        if file:
            self.run_async("Importing voice package", lambda: self._import_pack(file))

    def _import_pack(self, file):
        voice = self.lib.import_voice(Path(file), consent=True)
        self.emit("refresh", voice["id"])

    def export_personal_exe(self):
        """Create a real one-voice GUI EXE from the bundled, verified runtime."""
        from tkinter import filedialog
        from .single_export import locate_template, export_library_voice
        voice = self.selected_library_voice()
        if not voice:
            self.show_error("Select a voice from your library first.")
            return
        base = locate_template()
        if not base:
            self.show_error("SingleVoice-Base.exe is missing. Install the full studio from the release, or place the verified base EXE in the resources folder.")
            return
        destination = filedialog.asksaveasfilename(
            title="Create single-voice portable Windows EXE",
            defaultextension=".exe",
            initialfile="PocketVoice-" + voice["name"][:40] + ".exe",
            filetypes=[("Windows executable", "*.exe")])
        if destination:
            lang = self.lib.settings.get("language", "english_2026-04")
            self.run_async("Embedding voice into Windows EXE", lambda: export_library_voice(
                self.lib, voice["id"], base, destination, lang))

    def export_voice(self):
        from tkinter import filedialog, messagebox
        voice = self.selected_library_voice()
        if voice:
            path = filedialog.asksaveasfilename(defaultextension=".voicepack",
                    initialfile=voice["name"] + ".voicepack", filetypes=[("Voice pack", "*.voicepack")])
            if path:
                include = messagebox.askyesno("Portable model weights",
                    "Also include the selected language's cached Pocket model?\n\nThis can add hundreds of MB, but lets another PC import and generate without downloading models.\nChoose No for a smaller voice-only package.")
                self.run_async("Exporting voice package", lambda: self.lib.export_voice(
                    voice["id"], Path(path), include_models=include,
                    language=self.lib.settings.get("language", "english_2026-04")))

    def edit_voice(self):
        from tkinter import simpledialog
        voice = self.selected_library_voice()
        if not voice:
            return
        name = simpledialog.askstring("Voice name", "Name", initialvalue=voice["name"], parent=self.root)
        if name is None:
            return
        tags = simpledialog.askstring("Tags", "Comma-separated tags", initialvalue=voice.get("tags", ""), parent=self.root)
        if tags is None:
            return
        notes = simpledialog.askstring("Notes", "Notes", initialvalue=voice.get("notes", ""), parent=self.root)
        if notes is None:
            return
        self.lib.update_voice(voice["id"], name=name[:100], tags=tags[:250], notes=notes[:1000])
        self.refresh_voices()

    def favorite(self):
        voice = self.selected_library_voice()
        if voice:
            self.lib.update_voice(voice["id"], favorite=not voice.get("favorite", False))
            self.refresh_voices()

    def duplicate_voice(self):
        voice = self.selected_library_voice()
        if voice:
            self.lib.duplicate(voice["id"])
            self.refresh_voices()

    def delete_voice(self):
        from tkinter import messagebox
        voice = self.selected_library_voice()
        if voice and messagebox.askyesno("Delete voice", f'Delete {voice["name"]} and its stored references?'):
            self.lib.delete_voice(voice["id"])
            self.selected_voice = None
            self.refresh_voices()

    def paste_clipboard(self):
        try:
            pasted = self.root.clipboard_get()
            self.text.insert("insert", pasted)
        except self.tk.TclError:
            self.set_status("Clipboard has no text.")

    def render_settings(self):
        self.collect_settings()
        settings = copy.deepcopy(self.lib.settings)
        # Per-voice adjustments can optionally override the current global setup.
        return settings

    def generate(self):
        voice = self.lib.get_voice(self.selected_id())
        if not voice:
            self.show_error("Import or select a reference voice first.")
            return
        text = self.text.get("1.0", "end-1c").strip()
        if not text:
            self.show_error("Enter text to speak.")
            return
        self.selected_voice = voice["id"]
        config = self.render_settings()
        blend = self.lib.get_voice(self.menu_values.get(self.mix_var.get(), ""))
        amount = float(self.mix_percent.get())
        self.run_async("Generating speech", lambda: self.synthesize_job(text, voice, config, blend, amount))

    def speaker(self, samples, rate):
        if self.cancel.is_set():
            return
        device = self.output_device_id()
        with sd.OutputStream(samplerate=int(rate), channels=1, dtype="float32",
                             device=device, latency="high") as stream:
            self.active_stream = stream
            try:
                cursor = 0
                while cursor < len(samples) and not self.cancel.is_set():
                    piece = np.asarray(samples[cursor:cursor + int(rate * .25)], dtype="float32")
                    stream.write(piece.reshape(-1, 1))
                    cursor += len(piece)
                if self.cancel.is_set():
                    stream.abort(ignore_errors=True)
            finally:
                self.active_stream = None

    def synthesize_job(self, text, voice, config, blend=None, amount=0):
        params = parameters(config)
        # Stream only in unmodified single-voice mode; effects/blending require full resynthesis.
        direct = params["speed"] == 1 and params["pitch"] == 0 and params["clarity"] == 0 and not (blend and amount)
        chunks, buffered_frames = [], 0
        live_stream = None

        def streamed(chunk, rate):
            nonlocal buffered_frames, live_stream
            if self.cancel.is_set():
                return
            if live_stream is None:
                chunks.append(chunk)
                buffered_frames += len(chunk)
            if live_stream is None and buffered_frames >= rate * .7:
                live_stream = sd.OutputStream(samplerate=rate, channels=1,
                          dtype="float32", latency="high", device=self.output_device_id())
                live_stream.start()
                self.active_stream = live_stream
                for previous in chunks:
                    live_stream.write(previous.reshape(-1, 1))
                chunks.clear()
            elif live_stream is not None:
                live_stream.write(chunk.reshape(-1, 1))

        try:
            sound, rate = self.engine.synthesize(text, voice, config,
                                                  status=self.set_status, stop=self.cancel,
                                                  on_chunk=streamed if direct else None)
            if self.cancel.is_set():
                return
            if blend and amount > 0:
                self.set_status("Rendering second voice for experimental blend…")
                second, rate2 = self.engine.synthesize(text, blend, config,
                                                        status=self.set_status, stop=self.cancel)
                if self.cancel.is_set():
                    return
                if rate != rate2:
                    raise RuntimeError("Cannot blend voices with different sample rates.")
                sound = blend_audio(sound, second, amount)
            if not len(sound):
                raise RuntimeError("No audio generated.")
            if live_stream is not None:
                live_stream.stop()
            else:
                self.set_status("Playing finished audio…")
                self.speaker(sound, rate)
            if self.cancel.is_set():
                return
            self.lib.append_history(text, voice["id"], sound, rate)
            self.emit("completed", (sound, rate, text, voice["id"]))
        finally:
            if live_stream is not None:
                try:
                    if self.cancel.is_set():
                        live_stream.abort(ignore_errors=True)
                    live_stream.close()
                finally:
                    self.active_stream = None

    def playback_file(self, file):
        sound, rate = sf.read(file, dtype="float32", always_2d=True)
        self.speaker(sound.mean(axis=1), rate)

    def preview_voice(self):
        voice = self.selected_library_voice()
        if voice:
            self.run_async("Playing reference", lambda: self.playback_file(self.lib.condition_path(voice)))

    def replay_latest(self):
        """Replay the saved waveform, never run ONNX again."""
        if self.last_audio is None:
            self.show_error("Generate a sample once, then Replay works without regenerating.")
            return
        audio, rate = self.last_audio.copy(), self.last_rate
        self.run_async("Replaying saved audio", lambda: self.speaker(audio, rate))

    def export_latest(self):
        from tkinter import filedialog
        if self.last_audio is None:
            self.show_error("Generate speech before exporting.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".wav",
                                           filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3")])
        if path:
            self.render_settings()
            self.run_async("Exporting audio", lambda: export_audio(
                self.last_audio, self.last_rate, path,
                channels=int(self.lib.settings["channels"]),
                bitrate=int(self.lib.settings["mp3_bitrate"]),
                target_rate=int(self.lib.settings["sample_rate"])))

    def compare(self):
        voice = self.lib.get_voice(self.selected_id())
        text = self.text.get("1.0", "end-1c").strip()
        if not voice or not text:
            self.show_error("Select a voice and enter comparison text.")
            return
        config = self.render_settings()
        self.run_async("A/B comparison", lambda: self.compare_job(voice, text, config))

    def compare_job(self, voice, text, base):
        base.update({"speed": 1, "pitch": 0, "clarity": 0})
        files = []
        for index, preset in enumerate(("Balanced", "Ultra")):
            if self.cancel.is_set():
                return
            config = dict(base, preset=preset)
            self.set_status(f"Rendering {preset}…")
            audio, rate = self.engine.synthesize(text, voice, config, status=self.set_status, stop=self.cancel)
            if not len(audio):
                return
            file = self.lib.root / "history" / ("compare-" + str(time.time_ns()) + f"-{index}.wav")
            sf.write(file, audio, rate)
            files.append(file)
        self.emit("comparison", files)

    def play_compare(self, index):
        file = self.compare_files[index]
        if file and Path(file).is_file():
            self.run_async("A/B playback", lambda: self.playback_file(file))
        else:
            self.show_error("Run A/B comparison first.")

    def refresh_history(self):
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for row in self.lib.history:
            voice = self.lib.get_voice(row["voice_id"])
            self.history_tree.insert("", "end", iid=row["id"], values=(
                row.get("created", "")[:19].replace("T", " "),
                voice["name"] if voice else "Deleted voice", row.get("text", "")[:130].replace("\n", " ")))

    def history_row(self):
        selection = self.history_tree.selection()
        return next((r for r in self.lib.history if selection and r["id"] == selection[0]), None)

    def replay_history(self):
        row = self.history_row()
        if row:
            self.run_async("Replaying", lambda: self.playback_file(self.lib.root / row["path"]))

    def export_history(self):
        from tkinter import filedialog
        row = self.history_row()
        if row:
            destination = filedialog.asksaveasfilename(defaultextension=".wav",
                                filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3")])
            if destination:
                source = self.lib.root / row["path"]
                audio, rate = sf.read(source, dtype="float32")
                self.run_async("Exporting history", lambda: export_audio(audio, rate, destination,
                    channels=int(self.channel_var.get()), bitrate=int(self.bitrate_var.get()),
                    target_rate=int(self.sample_rate_var.get())))

    def reuse_history(self):
        row = self.history_row()
        if row:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", row["text"])
            self.selected_voice = row["voice_id"]
            self.refresh_voices()
            self.tabs.select(self.studio)

    def select_batch(self):
        from tkinter import filedialog, messagebox
        pick_folder = messagebox.askyesno("Batch selection", "Process all supported files in a folder? (No = pick files)")
        if pick_folder:
            folder = filedialog.askdirectory()
            choices = list(Path(folder).iterdir()) if folder else []
        else:
            choices = [Path(p) for p in filedialog.askopenfilenames()]
        self.batch_files = [str(f) for f in choices if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS | {".txt", ".md"}]
        for item in self.batch_tree.get_children():
            self.batch_tree.delete(item)
        for idx, path in enumerate(self.batch_files):
            self.batch_tree.insert("", "end", iid=str(idx), values=(Path(path).name, "Queued"))
        self.batch_label.configure(text=f"{len(self.batch_files)} supported items selected")

    def run_batch(self):
        voice = self.lib.get_voice(self.selected_id())
        if not voice or not self.batch_files:
            self.show_error("Select a Studio voice and batch files first.")
            return
        config = self.render_settings()
        self.run_async("Running batch", lambda: self.batch_job(voice, config))

    def batch_job(self, voice, config):
        folder = self.lib.root / "exports" / "batch"
        folder.mkdir(parents=True, exist_ok=True)
        for idx, raw in enumerate(self.batch_files):
            if self.cancel.is_set():
                break
            source = Path(raw)
            try:
                self.emit("batch_status", (idx, "Processing"))
                if source.suffix.lower() in TEXT_EXTENSIONS:
                    text = source.read_text(encoding="utf-8-sig")
                else:
                    text = transcribe_local(source, self.lib, self.set_status)
                audio, rate = self.engine.synthesize(text, voice, config,
                                                      status=self.set_status, stop=self.cancel)
                if not len(audio) or self.cancel.is_set():
                    return
                outfile = folder / f"{source.stem[:65]}-{idx+1:03d}.wav"
                export_audio(audio, rate, outfile,
                             channels=int(config["channels"]), target_rate=int(config["sample_rate"]))
                self.lib.append_history(text, voice["id"], audio, rate)
                self.emit("batch_status", (idx, "Saved " + outfile.name))
            except Exception as exc:
                LOG.exception("Batch processing failure for %s", source)
                self.emit("batch_status", (idx, "FAILED: " + str(exc)[:70]))
        self.emit("refresh_history", None)

    def start_record(self, mode):
        from tkinter import messagebox
        if self.record_stream:
            self.stop_record()
            return
        if self.worker and self.worker.is_alive():
            self.set_status("Stop the current generation before recording.")
            return
        try:
            if mode == "reference" and not self.consent.get():
                messagebox.showwarning("Consent required", "Confirm ownership or permission before recording a reference.")
                return
            input_info = sd.query_devices(kind="input")
            self.record_rate = int(input_info["default_samplerate"])
            self.record_frames = []
            self.record_mode = mode

            def callback(indata, frames, timing, flags):
                # PortAudio callback: keep it short and never touch Tk.
                if flags:
                    LOG.warning("Audio input warning: %s", flags)
                with self.record_lock:
                    self.record_frames.append(indata.copy())

            self.record_stream = sd.InputStream(samplerate=self.record_rate, channels=1,
                                                dtype="float32", callback=callback)
            self.record_stream.start()
            self.set_status("● Recording microphone. Press Mic again or release hotkey to finish.")
            self.update_floating()
        except Exception as exc:
            self.record_stream = None
            self.show_error("Microphone unavailable: " + str(exc))

    def stop_record(self):
        stream = self.record_stream
        if stream is None:
            return
        self.record_stream = None
        try:
            stream.stop()
            stream.close()
        except Exception:
            LOG.exception("Mic stop")
        with self.record_lock:
            frames = self.record_frames
            self.record_frames = []
        if not frames:
            self.set_status("No audio recorded.")
            return
        audio = np.concatenate(frames).reshape(-1)
        if len(audio) < self.record_rate * .7:
            self.set_status("Recording too short.")
            return
        if len(audio) > self.record_rate * 30 * 60:
            audio = audio[:self.record_rate * 30 * 60]
        file = self.lib.root / "imports" / ("microphone-" + str(int(time.time())) + ".wav")
        sf.write(file, audio, self.record_rate)
        mode = self.record_mode
        self.record_mode = None
        if mode == "reference":
            from tkinter import simpledialog
            name = simpledialog.askstring("New voice", "Name this recorded voice:", parent=self.root)
            if name:
                self.run_async("Importing microphone reference", lambda: self._add_voice(str(file), name))
        else:
            voice = self.lib.get_voice(self.selected_id())
            if not voice:
                self.show_error("Choose an output voice before speech-to-speech.")
                return
            config = self.render_settings()
            self.run_async("Transcribing local microphone", lambda: self.speech_job(file, voice, config))
        self.update_floating()

    def speech_job(self, file, voice, config):
        transcribed = transcribe_local(file, self.lib, self.set_status)
        self.emit("transcription", transcribed)
        if not self.cancel.is_set():
            self.synthesize_job(transcribed, voice, config)

    def stop(self):
        self.cancel.set()
        if self.record_stream:
            recording = self.record_stream
            self.record_stream = None
            self.record_mode = None
            try:
                recording.abort(ignore_errors=True)
                recording.close()
            except Exception:
                pass
            with self.record_lock:
                self.record_frames = []
        if self.active_stream:
            try:
                self.active_stream.abort(ignore_errors=True)
            except Exception:
                pass
        sd.stop()
        self.set_status("Stopped.")
        self.update_floating()

    def run_async(self, message, function):
        if self.worker and self.worker.is_alive():
            self.set_status("Another task is in progress. Press Stop first.")
            return
        self.cancel = threading.Event()
        self.set_status(message)
        if self.progressbar:
            self.progressbar.start(12)
        def execute():
            try:
                function()
            except Exception as exc:
                LOG.error("Task failure:\n%s", traceback.format_exc())
                if not self.cancel.is_set():
                    self.emit("error", f"{exc}\n\nDetails: {self.logfile}")
            finally:
                self.emit("idle", None)
        self.worker = threading.Thread(target=execute, daemon=True)
        self.worker.start()

    def emit(self, kind, value):
        self.events.put((kind, value))

    def set_status(self, status):
        self.emit("status", str(status)[:200])

    def poll(self):
        from tkinter import messagebox
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.current_label.set(value)
                    if self.floating:
                        self.mini_status.set(value[:46])
                elif kind == "error":
                    messagebox.showerror("Voice Studio error", value)
                    self.current_label.set("Error · see last-error.txt")
                elif kind == "idle":
                    if self.progressbar:
                        self.progressbar.stop()
                elif kind == "refresh":
                    self.selected_voice = value
                    self.refresh_voices()
                elif kind == "refresh_history":
                    self.refresh_history()
                elif kind == "completed":
                    self.last_audio, self.last_rate, self.last_text, self.last_voice_id = value
                    self.current_label.set(f"Finished · {len(self.last_audio)/self.last_rate:.1f}s of generated audio")
                    self.refresh_history()
                elif kind == "comparison":
                    self.compare_files = value
                    self.current_label.set("A/B files ready · open History to listen")
                    self.tabs.select(self.history_tab)
                elif kind == "transcription":
                    self.text.delete("1.0", "end")
                    self.text.insert("1.0", value)
                elif kind == "batch_status":
                    idx, info = value
                    if self.batch_tree.exists(str(idx)):
                        self.batch_tree.set(str(idx), "status", info)
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def show_error(self, error):
        from tkinter import messagebox
        messagebox.showwarning("Voice Clone Studio", error)

    def start_hotkey(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        try:
            from pynput import keyboard
            sequence = self.hotkey_var.get().strip()
            hotkey = keyboard.HotKey.parse(sequence)
            wanted = frozenset(hotkey)
            def trigger():
                def handle():
                    if self.hotkey_mode_var.get() == "toggle" and self.record_stream:
                        self.stop_record()
                    elif not self.record_stream:
                        self.start_record("speech")
                    self.hotkey_active = True
                self.root.after(0, handle)
            shortcut = keyboard.HotKey(wanted, trigger)

            def down(key):
                try:
                    shortcut.press(listener.canonical(key))
                except Exception:
                    LOG.exception("Hotkey press error")

            def up(key):
                normalized = listener.canonical(key)
                shortcut.release(normalized)
                if self.hotkey_mode_var.get() == "hold" and normalized in wanted and self.hotkey_active:
                    self.hotkey_active = False
                    self.root.after(0, self.stop_record)
            listener = keyboard.Listener(on_press=down, on_release=up)
            self.listener = listener
            listener.daemon = True
            listener.start()
            self.set_status(f"Global mic hotkey: {sequence} ({self.hotkey_mode_var.get()})")
        except Exception:
            LOG.exception("Global hotkey failed")
            self.set_status("Global hotkey unavailable; use Mic button in Studio.")

    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            pic = Image.new("RGB", (64, 64), "#101a2a")
            draw = ImageDraw.Draw(pic)
            draw.rounded_rectangle((23, 8, 41, 41), radius=9, fill="#4dd5f1")
            draw.arc((15, 16, 49, 53), 0, 180, fill="white", width=4)
            draw.line((32, 52, 32, 58), fill="white", width=4)
            self.tray = pystray.Icon("PortableVoiceClone", pic, "Voice Clone Studio",
                pystray.Menu(
                    pystray.MenuItem("Show Studio", lambda: self.root.after(0, self.show_main), default=True),
                    pystray.MenuItem("Mic record / stop", lambda: self.root.after(0, lambda: self.start_record("speech"))),
                    pystray.MenuItem("Floating panel", lambda: self.root.after(0, self.show_floating)),
                    pystray.MenuItem("Exit", lambda: self.root.after(0, self.force_quit)),
                ))
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            LOG.exception("Tray unavailable")

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def show_floating(self):
        if self.floating and self.floating.winfo_exists():
            self.floating.deiconify()
            self.floating.lift()
            return
        self.floating = self.tk.Toplevel(self.root)
        self.floating.title("Voice control")
        self.floating.geometry("350x242")
        self.floating.minsize(265, 78)
        self.floating.configure(bg=self.bg)
        self.floating.attributes("-topmost", True)
        self.mini_status = self.tk.StringVar(value="Ready")
        row = self.frame(self.floating, padding=(12, 8))
        row.pack(fill="x")
        self.label(row, "VOICE:", style="Sub.TLabel").pack(side="left")
        self.label(row, "", textvariable=self.voice_name).pack(side="left", padx=8)
        self.make_btn(row, "— Collapse", self.toggle_collapse).pack(side="right")
        self.mini_controls = self.frame(self.floating, padding=(8, 3))
        self.mini_controls.pack(fill="x")
        self.make_btn(self.mini_controls, "● Mic / Stop", lambda: self.stop_record() if self.record_stream else self.start_record("speech")).pack(side="left", padx=3)
        self.make_btn(self.mini_controls, "▶ Speak", self.generate).pack(side="left", padx=3)
        self.make_btn(self.mini_controls, "■", self.stop).pack(side="left", padx=3)
        self.mini_sliders = self.frame(self.floating, padding=(12, 2))
        self.mini_sliders.pack(fill="x")
        for title, var, low, high, resolution in (
            ("Speed", self.speed, .5, 2, .05),
            ("Stability", self.stability, 0, 100, 5),
        ):
            line = self.frame(self.mini_sliders)
            line.pack(fill="x")
            self.label(line, title).pack(side="left")
            self.tk.Scale(line, from_=low, to=high, resolution=resolution,
                          variable=var, orient="horizontal", showvalue=True,
                          length=190, bg=self.bg, fg=self.fg,
                          highlightthickness=0, troughcolor=self.card).pack(side="right")
        self.label(self.floating, "", textvariable=self.mini_status).pack(anchor="w", padx=12, pady=5)
        self.floating.protocol("WM_DELETE_WINDOW", self.floating.withdraw)
        self.update_floating()

    def update_floating(self):
        if self.floating and self.floating.winfo_exists():
            voice = self.lib.get_voice(self.selected_voice)
            self.voice_name.set(voice["name"][:20] if voice else "None")

    def toggle_collapse(self):
        if not self.floating:
            return
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.mini_controls.pack_forget()
            self.mini_sliders.pack_forget()
            self.floating.geometry("350x78")
        else:
            self.mini_controls.pack(fill="x", before=self.floating.winfo_children()[-1])
            self.mini_sliders.pack(fill="x", before=self.floating.winfo_children()[-1])
            self.floating.geometry("350x242")

    def edit_dictionary(self):
        from tkinter import messagebox
        window = self.tk.Toplevel(self.root)
        window.title("Pronunciation replacements · one per line")
        window.geometry("560x390")
        self.label(window, "One entry per line: difficult word => phonetic spelling").pack(pady=8)
        area, entry = self.text_area(window, height=14)
        area.pack(fill="both", expand=True, padx=12)
        entry.insert("1.0", "\n".join(k + " => " + v for k, v in self.lib.dictionary.items()))
        def save():
            mapping = {}
            for line in entry.get("1.0", "end-1c").splitlines():
                if not line.strip():
                    continue
                if "=>" not in line:
                    messagebox.showerror("Invalid dictionary", f"Expected word => replacement:\n{line}")
                    return
                k, v = (x.strip() for x in line.split("=>", 1))
                if not k or not v:
                    return
                mapping[k] = v
            self.lib.dictionary = mapping
            self.lib.save()
            window.destroy()
        self.make_btn(window, "Save replacements", save, primary=True).pack(pady=10)

    def save_profile(self):
        from tkinter import simpledialog
        title = simpledialog.askstring("Save profile", "Profile name:", parent=self.root)
        if title:
            self.render_settings()
            self.lib.profiles[title.strip()] = {"voice_id": self.selected_id(),
                                                "settings": copy.deepcopy(self.lib.settings)}
            self.lib.save()
            self.set_status("Profile saved.")

    def load_profile(self):
        from tkinter import simpledialog
        if not self.lib.profiles:
            self.show_error("No profiles saved.")
            return
        title = simpledialog.askstring("Load profile",
                    "Enter a profile name:\n" + "\n".join(self.lib.profiles.keys()), parent=self.root)
        if title in self.lib.profiles:
            profile = self.lib.profiles[title]
            self.lib.settings.update(profile["settings"])
            self.selected_voice = profile["voice_id"]
            for var, setting in ((self.preset, "preset"), (self.speed, "speed"),
                                 (self.pitch, "pitch"), (self.stability, "stability"), (self.clarity, "clarity")):
                var.set(self.lib.settings[setting])
            self.refresh_voices()
            self.set_status(f"Profile loaded: {title}")

    def close(self):
        if self.tray and self.lib.settings.get("minimize_to_tray", False):
            self.root.withdraw()
            self.set_status("Running in system tray.")
        else:
            self.force_quit()

    def force_quit(self):
        self.cancel.set()
        if self.listener:
            self.listener.stop()
        if self.record_stream:
            try:
                self.record_stream.abort(ignore_errors=True)
                self.record_stream.close()
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Pocket TTS portable voice studio")
    parser.add_argument("--self-test", action="store_true", help="Verify frozen runtime dependencies")
    parser.add_argument("--speak-text")
    parser.add_argument("--speak-file")
    parser.add_argument("--voice-id")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.self_test or args.speak_text or args.speak_file:
        return cli_main(args)
    Studio().run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if getattr(sys, "frozen", False):
            # A windowed exe may have no console. Show diagnostics, never silently exit.
            import ctypes
            location = setup_log(data_home())
            LOG.error("Fatal startup failure:\n%s", traceback.format_exc())
            ctypes.windll.user32.MessageBoxW(0, f"Voice studio failed to start. See:\n{location}",
                                             "Voice Clone Studio", 0x10)
        else:
            raise
