"""Single personalized voice Windows GUI.

Launches from a native AOT EXE with one embedded WAV and an extracted frozen
Pocket inference runtime. No Python installation is required by end users.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import (
    Tk, Toplevel, StringVar, BooleanVar, DoubleVar, Text, Frame, Label,
    Button, Checkbutton, Scale, END, filedialog, messagebox, ttk
)

import numpy as np
import sounddevice as sd

from .core import Library, PocketEngine, PRESETS, parameters, export_audio, now

DARK = "#0d1526"
PANEL = "#19263d"
WHITE = "#eef6ff"
CYAN = "#73dff5"
MUTED = "#a2b7d2"
MODEL_LANGUAGES = {"english_2026-04", "english", "german", "spanish", "italian", "portuguese"}


def read_profile(path: Path):
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    ref = Path(str(profile.get("voice_path", "")))
    if not ref.is_file() or ref.suffix.lower() != ".wav":
        raise ValueError("This personalized executable is missing its voice WAV.")
    language = str(profile.get("lang", "english_2026-04"))
    if language not in MODEL_LANGUAGES:
        raise ValueError("This voice is configured with an unsupported language.")
    return {"name": str(profile.get("voice_name") or "My cloned voice")[:72],
            "lang": language, "voice_path": ref}


class CompactStudio:
    def __init__(self, config_path):
        self.profile = read_profile(Path(config_path))
        # Desktop and personalized voices share the user-local inference cache.
        base = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PortableVoiceClone" / "VoiceData"
        self.lib = Library(base)
        self.engine = PocketEngine(self.lib)
        self.voice = {"id": "personalized", "name": self.profile["name"],
                      "reference": str(self.profile["voice_path"])}
        # Library paths are normally relative to root. For a signed/extracted
        # profile the voice_path is absolute; Path(base) / absolute stays absolute.
        self.root = Tk()
        self.root.title(self.profile["name"] + " · Pocket Voice")
        self.root.geometry("704x600")
        self.root.minsize(530, 470)
        self.root.configure(bg=DARK)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.task = None
        self.active_stream = None
        self.last_audio = None
        self.last_rate = 24000
        self.last_text = None
        self.voice_name = StringVar(value=self.profile["name"])
        self.status = StringVar(value="Ready · single voice · local Pocket TTS")
        self.preset = StringVar(value="Balanced")
        self.speed = DoubleVar(value=1.0)
        self.pitch = DoubleVar(value=0.0)
        self.stability = DoubleVar(value=50)
        self.topmost = BooleanVar(value=False)
        self._style()
        self._build()
        self.root.after(100, self._poll)
        self.root.bind("<Control-Return>", lambda event: self.generate())
        self.root.bind("<Control-s>", lambda event: self.export_audio())
        self.root.bind("<Escape>", lambda event: self.stop())

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", foreground=WHITE, fieldbackground=PANEL, background=PANEL,
                        bordercolor="#34516d", arrowcolor=CYAN, padding=(9, 6))
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
                  selectbackground=[("readonly", PANEL)],
                  selectforeground=[("readonly", WHITE)])

    def btn(self, parent, text, action, primary=False):
        b = Button(parent, text=text, command=action, cursor="hand2", relief="flat",
                   bg=CYAN if primary else PANEL, fg=DARK if primary else WHITE,
                   activebackground="#baf3ff" if primary else "#2c4262",
                   font=("Segoe UI Semibold", 10), padx=13, pady=8,
                   highlightthickness=0, bd=0)
        return b

    def _label(self, parent, text, fg=MUTED, size=10, bold=False):
        return Label(parent, text=text, bg=DARK, fg=fg,
                     font=("Segoe UI Semibold" if bold else "Segoe UI", size))

    def _build(self):
        outer = Frame(self.root, bg=DARK)
        outer.pack(fill="both", expand=True, padx=20, pady=16)

        self._label(outer, "POCKET VOICE  /  PERSONAL EDITION", CYAN, 10, True).pack(anchor="w")
        name = self._label(outer, "", WHITE, 23, True)
        name.configure(textvariable=self.voice_name, wraplength=580, justify="left")
        name.pack(anchor="w", pady=(6, 2))
        self._label(outer, "Your saved voice is built into this EXE. Type, generate, replay or export.").pack(anchor="w", pady=(0, 15))

        self._label(outer, "TEXT TO SPEAK", CYAN, 10, True).pack(anchor="w", pady=(0, 7))
        self.text = Text(outer, height=9, wrap="word", relief="flat", bd=0,
                         bg=PANEL, fg=WHITE, insertbackground=WHITE, selectbackground="#285776",
                         padx=15, pady=14, font=("Segoe UI", 11), undo=True)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", "Hello! This is my personalized Pocket TTS voice. I can replay this result without regenerating it.")

        row = Frame(outer, bg=DARK)
        row.pack(fill="x", pady=(12, 12))
        self.btn(row, "▶  Generate", self.generate, True).pack(side="left", padx=(0, 7))
        self.btn(row, "↻  Replay", self.replay).pack(side="left", padx=3)
        self.btn(row, "↓  WAV / MP3", self.export_audio).pack(side="left", padx=3)
        self.btn(row, "■  Stop", self.stop).pack(side="right")

        cols = Frame(outer, bg=DARK)
        cols.pack(fill="x", pady=(4, 6))
        self._label(cols, "QUALITY", CYAN, 10, True).pack(side="left")
        menu = ttk.Combobox(cols, textvariable=self.preset, state="readonly",
                            values=list(PRESETS), width=17)
        menu.pack(side="left", padx=(12, 18))
        Checkbutton(cols, text="Always on top", variable=self.topmost, command=self._set_topmost,
                    bg=DARK, fg=WHITE, activebackground=DARK, activeforeground=WHITE,
                    selectcolor=PANEL, font=("Segoe UI", 9)).pack(side="left")

        controls = Frame(outer, bg=DARK)
        controls.pack(fill="x")
        for caption, var, lo, hi, step in [
            ("Speed", self.speed, 0.5, 2, 0.05),
            ("Pitch", self.pitch, -8, 8, 1),
            ("Stability", self.stability, 0, 100, 5),
        ]:
            section = Frame(controls, bg=DARK)
            section.pack(side="left", expand=True, fill="x")
            self._label(section, caption, MUTED, 9).pack(anchor="w")
            Scale(section, variable=var, from_=lo, to=hi, resolution=step, orient="horizontal",
                  showvalue=True, length=175, bg=DARK, fg=WHITE, troughcolor=PANEL,
                  highlightthickness=0, bd=0).pack(anchor="w")

        Label(outer, textvariable=self.status, bg=DARK, fg=CYAN, anchor="w",
              justify="left", wraplength=640, font=("Segoe UI", 9)).pack(fill="x", pady=(10, 0))
        self._label(outer, "Ctrl+Enter generate  ·  Ctrl+S export  ·  Esc stop  ·  100% local synthesis after model download",
                    MUTED, 8).pack(anchor="w", pady=(6, 0))

    def _set_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def _emit(self, kind, value):
        self.events.put((kind, value))

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status.set(str(value))
                elif kind == "ready":
                    self.last_audio, self.last_rate, self.last_text = value
                    self.status.set(f"Saved last generation in memory · {len(self.last_audio)/self.last_rate:.1f}s · Replay any time")
                elif kind == "error":
                    self.status.set("Generation failed · see error")
                    messagebox.showerror("Pocket voice error", str(value))
                elif kind == "finish":
                    self.task = None
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _run(self, job):
        if self.task and self.task.is_alive():
            self.status.set("Generation still in progress. Press Stop before starting another.")
            return
        self.stop_event = threading.Event()

        def runner():
            try:
                job()
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._emit("error", f"{exc}\n\nSee %LOCALAPPDATA%\\PortableVoiceClone\\VoiceData\\single-voice-error.txt")
                try:
                    path = self.lib.root / "single-voice-error.txt"
                    path.write_text(traceback.format_exc(), encoding="utf-8")
                except OSError:
                    pass
            finally:
                self._emit("finish", None)
        self.task = threading.Thread(target=runner, daemon=True)
        self.task.start()

    def settings(self):
        return {"preset": self.preset.get(), "speed": float(self.speed.get()),
                "pitch": float(self.pitch.get()), "stability": float(self.stability.get()),
                "clarity": 0, "language": self.profile["lang"], "safe_mode": True}

    def play(self, samples, rate):
        stream = None
        try:
            stream = sd.OutputStream(samplerate=int(rate), channels=1,
                                     dtype="float32", latency="high")
            self.active_stream = stream
            stream.start()
            for start in range(0, len(samples), max(1, int(rate * .25))):
                if self.stop_event.is_set():
                    break
                segment = np.asarray(samples[start:start+int(rate * .25)], dtype=np.float32)
                stream.write(segment.reshape(-1, 1))
            if self.stop_event.is_set():
                stream.abort(ignore_errors=True)
            else:
                stream.stop()
        finally:
            if stream:
                stream.close()
            self.active_stream = None

    def generate(self):
        phrase = self.text.get("1.0", "end-1c").strip()
        if not phrase:
            messagebox.showwarning("Empty input", "Enter some text first.")
            return
        config = self.settings()
        self._run(lambda: self._generate_job(phrase, config))

    def _generate_job(self, phrase, config):
        self._emit("status", "Preparing Pocket TTS · model cache is checked locally…")
        blocks = []
        current = 0
        stream = None
        modified = parameters(config)
        direct = modified["speed"] == 1 and modified["pitch"] == 0 and modified["clarity"] == 0

        def on_chunk(pcm, hz):
            nonlocal current, stream
            if self.stop_event.is_set():
                return
            if stream is None:
                blocks.append(pcm)
                current += len(pcm)
                if current >= hz * .6:
                    stream = sd.OutputStream(samplerate=hz, channels=1, dtype="float32", latency="high")
                    stream.start()
                    self.active_stream = stream
                    for block in blocks:
                        stream.write(block.reshape(-1, 1))
                    blocks.clear()
                    self._emit("status", "Streaming cloned speech…")
            else:
                stream.write(pcm.reshape(-1, 1))

        try:
            audio, rate = self.engine.synthesize(phrase, self.voice, config,
                 status=lambda message: self._emit("status", message),
                 stop=self.stop_event, on_chunk=on_chunk if direct else None)
            if self.stop_event.is_set() or not len(audio):
                return
            if stream is not None:
                stream.stop()
            else:
                self._emit("status", "Playing synthesized speech…")
                self.play(audio, rate)
            if not self.stop_event.is_set():
                self._emit("ready", (audio, rate, phrase))
        finally:
            if stream:
                if self.stop_event.is_set():
                    stream.abort(ignore_errors=True)
                stream.close()
                self.active_stream = None

    def replay(self):
        if self.last_audio is None:
            messagebox.showinfo("Nothing to replay", "Generate a speech sample first.")
            return
        self._run(lambda: self.play(self.last_audio, self.last_rate))

    def export_audio(self):
        if self.last_audio is None:
            messagebox.showinfo("Nothing to export", "Generate speech first, then save the existing audio.")
            return
        destination = filedialog.asksaveasfilename(
            title="Export generated speech", defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3")],
            initialfile="voice-output.wav",
        )
        if not destination:
            return
        audio, rate = self.last_audio.copy(), self.last_rate
        self._run(lambda: self._save_job(audio, rate, destination))

    def _save_job(self, audio, rate, destination):
        export_audio(audio, rate, destination, channels=1, bitrate=192, target_rate=rate)
        self._emit("status", f"Exported: {destination}")

    def stop(self):
        self.stop_event.set()
        stream = self.active_stream
        if stream:
            try:
                stream.abort(ignore_errors=True)
            except Exception:
                pass
        sd.stop()
        self.status.set("Stopped. Last completed audio is still available for Replay / Export.")

    def close(self):
        self.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        # Validate frozen runtime import, not audio playback.
        import imageio_ffmpeg
        import pocket_tts_onnx
        assert imageio_ffmpeg.get_ffmpeg_exe()
        return 0
    if not args.config_file:
        raise SystemExit("A personalized voice profile is required. Export an EXE from the website or desktop suite.")
    CompactStudio(args.config_file).run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if getattr(sys, "frozen", False):
            import ctypes
            root = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PortableVoiceClone"
            root.mkdir(parents=True, exist_ok=True)
            log = root / "single-startup-error.txt"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            ctypes.windll.user32.MessageBoxW(0, f"Single-voice studio could not start.\n\nSee {log}",
                "Pocket Voice - Error", 0x10)
            sys.exit(1)
        raise
