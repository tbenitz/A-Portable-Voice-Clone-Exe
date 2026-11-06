"""Single-use, ONNX-only Pocket TTS clipboard reader.

Models are downloaded once from KevinAHM/pocket-tts-onnx (public CC BY 4.0).
The reference WAV and clipboard text remain on the Windows device.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

MODEL_REPO = "KevinAHM/pocket-tts-onnx"
MODEL_REVISION = "58a6d00cf13d239b6748cb0769f35c580a8f606c"
MODEL_FILES = (
    "bundle.json",
    "tokenizer.model",
    "bos_before_voice.npy",
    "mimi_encoder.onnx",
    "text_conditioner.onnx",
    "flow_lm_main_int8.onnx",
    "flow_lm_flow_int8.onnx",
    "mimi_decoder_int8.onnx",
)
LANGUAGES = {"english_2026-04", "english", "german", "spanish", "italian", "portuguese"}


def split_text(text: str, size: int = 250):
    """Split by sentence boundaries for manageable inference requests."""
    chunks = []
    for para in re.split(r"(?<=\n)", text):
        rest = para.strip()
        while rest:
            if len(rest) <= size:
                chunks.append(rest)
                break
            pos = max(
                rest.rfind(". ", 0, size + 1),
                rest.rfind("! ", 0, size + 1),
                rest.rfind("? ", 0, size + 1),
            )
            if pos > size // 3:
                cut = pos + 1
            else:
                cut = rest.rfind(" ", 0, size + 1)
                if cut <= size // 3:
                    cut = size
            chunks.append(rest[:cut].strip())
            rest = rest[cut:].lstrip()
    return [s for s in chunks if s]


def load_profile(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def model_cache_root():
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "ClipboardVoiceStudio" / "onnx_models"


def language_folder(language):
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported Pocket TTS language: {language}")
    return "english_2026-04" if language == "english" else language


def model_file_names(language):
    lang = language_folder(language)
    return [f"onnx/{lang}/{filename}" for filename in MODEL_FILES]


def configure_headless_runtime():
    """Keep library downloads working inside a PyInstaller --windowed EXE.

    A no-console executable has sys.stdout/sys.stderr = None. tqdm and some
    model libraries expect writable streams and can otherwise crash before
    inference starts. Disable CLI progress output and supply safe sinks.
    """
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not callable(getattr(stream, "write", None)):
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def ensure_public_models(root, language, progress, stopped):
    """Download public, version-pinned ONNX assets into a persistent per-user cache.

    No Hugging Face account or token is needed. Only model weights are downloaded.
    The user's clipboard and voice sample are never sent to the Hub.
    """
    if stopped.is_set():
        return None
    configure_headless_runtime()
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import disable_progress_bars

    # A CLI-style tqdm bar serves no purpose in the floating STOP window.
    # This call also protects frozen apps whose stderr is initially None.
    disable_progress_bars()

    root = Path(root)
    paths = model_file_names(language)
    for index, name in enumerate(paths, 1):
        if stopped.is_set():
            return None
        target = root / name
        if target.exists() and target.stat().st_size > 0:
            continue
        progress(f"Downloading ONNX model ({index}/{len(paths)})")
        hf_hub_download(
            repo_id=MODEL_REPO,
            filename=name,
            revision=MODEL_REVISION,
            local_dir=str(root),
            token=False,
        )
    return root / "onnx"


def run():
    configure_headless_runtime()
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test-headless":
        # Executed by GitHub Actions inside the real PyInstaller --windowed
        # binary, where stdout/stderr are absent. Exercise the same progress
        # setup that failed in the user's Windows screenshot.
        from huggingface_hub.utils.tqdm import tqdm as hub_tqdm, disable_progress_bars
        disable_progress_bars()
        with hub_tqdm(total=1, desc="Headless download self-check") as bar:
            bar.update(1)
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--config-file", required=True)
    args = parser.parse_args()

    text = Path(args.input_file).read_text(encoding="utf-8-sig").strip()
    config = load_profile(args.config_file)
    if not text:
        return

    stopped = threading.Event()
    messages = queue.Queue()
    active_stream = {"stream": None}

    root = tk.Tk()
    root.title("Clipboard Voice")
    root.configure(bg="#111827")
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    width, height = 235, 78
    root.update_idletasks()
    x = max(0, root.winfo_screenwidth() - width - 25)
    y = max(0, root.winfo_screenheight() - height - 74)
    root.geometry(f"{width}x{height}+{x}+{y}")
    label = tk.Label(root, text="Preparing local speech…", fg="#e5e7eb",
                     bg="#111827", font=("Segoe UI", 9), anchor="w")
    label.pack(fill="x", padx=12, pady=(8, 2))
    button = tk.Button(root, text="■  STOP", font=("Segoe UI", 11, "bold"),
                       fg="white", bg="#d9364e", activebackground="#b91c39",
                       relief="flat")
    button.pack(fill="x", padx=12, pady=(0, 8))

    def halt():
        if stopped.is_set():
            return
        stopped.set()
        stream = active_stream.get("stream")
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        try:
            root.destroy()
        except tk.TclError:
            pass

    button.configure(command=halt)
    root.bind("<Escape>", lambda event: halt())
    root.protocol("WM_DELETE_WINDOW", halt)

    def status(message):
        messages.put(("status", message))

    def play_segment(tts, voice_embeddings, segment):
        """Generate and play asynchronously with a short prebuffer and real drain.

        A daemon producer runs ONNX so speech generation can stay ahead of
        PortAudio playback. stream.stop() drains the last queued audio samples.
        """
        import numpy as np
        import sounddevice as sd

        audio_queue = queue.Queue(maxsize=16)

        def offer(item):
            while not stopped.is_set():
                try:
                    audio_queue.put(item, timeout=0.15)
                    return
                except queue.Full:
                    continue

        def producer():
            try:
                for block in tts.stream(segment, voice=voice_embeddings):
                    if stopped.is_set():
                        break
                    pcm = np.asarray(block, dtype=np.float32).reshape(-1)
                    if pcm.size:
                        offer(("data", pcm))
                offer(("done", None))
            except Exception as exc:
                offer(("error", exc))

        threading.Thread(target=producer, daemon=True).start()
        buffered = []
        buffered_frames = 0
        speaker = None

        def open_speaker():
            nonlocal speaker, buffered, buffered_frames
            speaker = sd.OutputStream(
                samplerate=int(tts.sample_rate), channels=1,
                dtype="float32", latency="high",
            )
            active_stream["stream"] = speaker
            speaker.start()
            status("Speaking…")
            for samples in buffered:
                if stopped.is_set():
                    return
                speaker.write(samples.reshape(-1, 1))
            buffered = []
            buffered_frames = 0

        try:
            while not stopped.is_set():
                try:
                    kind, value = audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "error":
                    raise value
                if kind == "done":
                    if speaker is None and buffered and not stopped.is_set():
                        open_speaker()
                    break
                if speaker is None:
                    buffered.append(value)
                    buffered_frames += value.size
                    if buffered_frames >= tts.sample_rate * 0.55:
                        open_speaker()
                else:
                    speaker.write(value.reshape(-1, 1))

            if speaker is not None and not stopped.is_set():
                # This drains the queued output before declaring speech finished.
                speaker.stop()
        finally:
            if speaker is not None:
                try:
                    if stopped.is_set():
                        speaker.abort()
                    speaker.close()
                except Exception:
                    pass
                active_stream["stream"] = None

    def worker():
        try:
            from pocket_tts_onnx import PocketTTSOnnx
            import numpy as np

            voice_path = Path(str(config.get("voice_path") or ""))
            if not voice_path.is_file():
                raise ValueError("No cloned voice sample was embedded in this EXE.")
            language = str(config.get("lang", "english_2026-04"))
            models_root = ensure_public_models(
                model_cache_root(), language, status, stopped
            )
            if stopped.is_set():
                return
            if models_root is None:
                return
            status("Loading Pocket TTS ONNX model…")
            tts = PocketTTSOnnx(
                models_dir=str(models_root),
                language=language_folder(language),
                precision="int8",
                device="cpu",
                lsd_steps=1,
            )
            if stopped.is_set():
                return

            status("Cloning your voice locally…")
            voice_embeddings = tts.encode_voice(voice_path)
            if not np.asarray(voice_embeddings).size:
                raise RuntimeError("The reference recording contained no usable speech.")

            chunks = split_text(text)
            for index, segment in enumerate(chunks, 1):
                if stopped.is_set():
                    return
                status(f"Generating speech {index}/{len(chunks)}…")
                play_segment(tts, voice_embeddings, segment)
            if not stopped.is_set():
                messages.put(("done", None))
        except Exception:
            if stopped.is_set():
                return
            details = traceback.format_exc()
            try:
                folder = model_cache_root().parent
                folder.mkdir(parents=True, exist_ok=True)
                log_path = folder / "last-error.txt"
                log_path.write_text(details, encoding="utf-8")
                summary = details.strip().splitlines()[-1]
                message = (
                    f"{summary}\n\nDetails saved to:\n{log_path}\n\n"
                    "The application will close after you dismiss this message."
                )
            except Exception:
                message = details[-1800:]
            messages.put(("error", message))

    threading.Thread(target=worker, daemon=True).start()

    def poll():
        while True:
            try:
                kind, value = messages.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                halt()
                return
            if kind == "error":
                root.withdraw()
                try:
                    messagebox.showerror("Pocket TTS could not speak", value, parent=root)
                finally:
                    halt()
                return
            label.config(text=value[:36])
        if not stopped.is_set():
            root.after(125, poll)

    root.after(100, poll)
    root.mainloop()


if __name__ == "__main__":
    run()
