"""Core library, signal processing, model integration and portable storage.

All user text, references, exports and local ASR remain on the machine.
The only network activity is optional first-run downloads of model weights.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

import numpy as np
import soundfile as sf

from .upstream_reader import (
    MODEL_REPO, MODEL_REVISION, ensure_public_models, language_folder, split_text
)

PRESETS = {
    "Real-time": {"steps": 1, "temperature": 0.52},
    "Balanced": {"steps": 2, "temperature": 0.65},
    "High quality": {"steps": 4, "temperature": 0.70},
    "Ultra": {"steps": 8, "temperature": 0.70},
}
DEFAULT_SETTINGS = {
    "preset": "Balanced", "speed": 1.0, "pitch": 0.0, "stability": 50,
    "clarity": 0, "language": "english_2026-04",
    "hotkey": "<ctrl>+<alt>+v", "hotkey_mode": "toggle", "minimize_to_tray": False,
    "theme": "dark", "sample_rate": 24000, "channels": 1, "mp3_bitrate": 192,
    "output_device": "", "history_limit": 50, "safe_mode": True,
    "mix_voice": "", "mix_percent": 0, "transcription_model": "tiny",
}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aiff", ".aac", ".wma"}
TEXT_EXTENSIONS = {".txt", ".md"}
FORMAT_VERSION = 1


def data_home() -> Path:
    override = os.getenv("VOICE_STUDIO_HOME")
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    executable_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    portable = executable_dir / "VoiceData"
    try:
        portable.mkdir(parents=True, exist_ok=True)
        test = portable / ".write-probe"
        test.write_bytes(b"1")
        test.unlink()
        return portable
    except OSError:
        fallback = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "PortableVoiceClone" / "VoiceData"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError, OSError):
        return copy.deepcopy(default)


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, float(value)))


def parameters(settings):
    preset = PRESETS.get(settings.get("preset"), PRESETS["Balanced"])
    stability = clamp(settings.get("stability", 50), 0, 100)
    # Higher stability => less sampling variance; this is an approximation, not speaker lock.
    temperature = clamp(preset["temperature"] * (1.4 - stability / 125), 0.1, 1.25)
    return {
        "steps": preset["steps"], "temperature": temperature,
        "speed": clamp(settings.get("speed", 1), .5, 2),
        "pitch": clamp(settings.get("pitch", 0), -8 if settings.get("safe_mode", True) else -12, 8 if settings.get("safe_mode", True) else 12),
        "clarity": clamp(settings.get("clarity", 0), 0, 100),
    }


def ffmpeg_executable():
    from imageio_ffmpeg import get_ffmpeg_exe
    return get_ffmpeg_exe()


def run_ffmpeg(args: list[str], timeout=180):
    cmd = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)]
    completed = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError("FFmpeg: " + completed.stderr.decode("utf-8", "replace")[-1600:])


def convert_audio(source: Path, dest: Path, sample_rate=24000, channels=1, start=None, duration=None, clarity=0):
    args = []
    if start is not None:
        args += ["-ss", f"{float(start):.3f}"]
    args += ["-i", str(source)]
    if duration is not None:
        args += ["-t", f"{float(duration):.3f}"]
    filters = ["highpass=f=75", "lowpass=f=9800"]
    if clarity:
        filters.append(f"afftdn=nr={int(5 + 15 * clamp(clarity, 0, 100) / 100)}")
    args += ["-vn", "-af", ",".join(filters), "-ar", str(sample_rate),
             "-ac", str(channels), "-c:a", "pcm_s16le", str(dest)]
    run_ffmpeg(args)


def signal_metrics(samples: np.ndarray):
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not x.size:
        return {"score": 0, "rms_db": -120, "peak_db": -120, "active": 0, "clipping": 0, "hint": "No audio"}
    # An approximate signal-integrity score, not a speaker-identity metric.
    x = x[np.isfinite(x)]
    if not x.size:
        return {"score": 0, "rms_db": -120, "peak_db": -120, "active": 0, "clipping": 0, "hint": "No finite samples"}
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    clipping = float(np.mean(np.abs(x) >= .985))
    frame_size = min(2400, max(1, len(x) // 20))
    usable = len(x) // frame_size * frame_size
    frames = x[:usable].reshape(-1, frame_size)
    energy = np.sqrt(np.mean(frames * frames, axis=1))
    # Speech heuristic: louder than -42dBFS; quiet ambience alone can appear active.
    active = float(np.mean(energy >= .008)) if len(energy) else 0.
    rms_db = 20 * math.log10(max(1e-6, rms))
    peak_db = 20 * math.log10(max(1e-6, peak))
    loudness = max(0, 1 - abs(rms_db + 23) / 28)
    score = int(round(100 * (.52 * loudness + .38 * active + .10 * (1 - min(1, clipping * 100)))))
    if rms_db < -39 or active < .18:
        hint = "Too quiet or too much silence; record closer to the microphone."
    elif clipping > .01:
        hint = "Clipping detected; lower recording gain."
    elif score < 58:
        hint = "Mixed signal quality; use a clean single-speaker passage."
    else:
        hint = "Usable signal. Check for other speakers, music and reverberation."
    return {"score": score, "rms_db": round(rms_db, 1), "peak_db": round(peak_db, 1),
            "active": round(active, 3), "clipping": round(clipping, 4), "hint": hint}


def inspect_reference(path: Path, max_minutes=45):
    """Inspect long recordings incrementally; select best 12-20s *window*."""
    source = Path(path)
    if source.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("Unsupported input audio type")
    # Decode unsupported formats once for analysis. Only the temporary decoded copy is discarded.
    temporary = None
    try:
        try:
            reader = sf.SoundFile(str(source))
        except (RuntimeError, OSError):
            temporary = tempfile.TemporaryDirectory()
            source = Path(temporary.name) / "decoded.wav"
            convert_audio(path, source, sample_rate=16000)
            reader = sf.SoundFile(str(source))
        with reader:
            rate, frames, channels = reader.samplerate, reader.frames, reader.channels
            if frames < rate:
                raise ValueError("Reference must contain at least one second of sound")
            seconds = frames / rate
            step_secs = 12.0
            window = min(18.0, max(1.0, seconds))
            step = max(1, int(rate * step_secs))
            best = None
            start_frame = 0
            cap = min(frames, int(max_minutes * 60 * rate))
            while start_frame < cap:
                reader.seek(start_frame)
                block = reader.read(frames=min(int(window * rate), cap - start_frame),
                                    dtype="float32", always_2d=True)
                mono = block.mean(axis=1)
                metrics = signal_metrics(mono[::max(1, rate // 12000)])
                # Prefer active sound not silence/clipping; score does not detect speaker identity.
                candidate = (metrics["score"], start_frame / rate, metrics)
                if best is None or candidate[0] > best[0]:
                    best = candidate
                start_frame += step
            if not best:
                raise ValueError("No usable reference window")
            return {"duration_seconds": round(seconds, 1), "sample_rate": rate,
                    "channels": channels, "selected_start": round(best[1], 2),
                    "selected_length": round(min(window, seconds - best[1]), 2),
                    **best[2]}
    finally:
        if temporary:
            temporary.cleanup()


class Library:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or data_home())
        for path in ("voices", "history", "imports", "exports", "models", "cache"):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"
        self.index_path = self.root / "voices.json"
        self.profiles_path = self.root / "profiles.json"
        self.dictionary_path = self.root / "pronunciation.json"
        self.history_path = self.root / "history.json"
        self.settings = {**DEFAULT_SETTINGS, **load_json(self.settings_path, {})}
        self.voices = load_json(self.index_path, [])
        self.profiles = load_json(self.profiles_path, {})
        self.dictionary = load_json(self.dictionary_path, {})
        self.history = load_json(self.history_path, [])

    def save(self):
        save_json(self.settings_path, self.settings)
        save_json(self.index_path, self.voices)
        save_json(self.profiles_path, self.profiles)
        save_json(self.dictionary_path, self.dictionary)
        save_json(self.history_path, self.history)

    def get_voice(self, voice_id):
        return next((v for v in self.voices if v["id"] == voice_id), None)

    def condition_path(self, voice):
        return self.root / voice["reference"]

    def add_voice(self, source, name, tags="", notes="", clarity=0, consent=False):
        if not consent:
            raise ValueError("Confirm that you own or have consent to use this voice.")
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        health = inspect_reference(source)
        voice_id = uuid.uuid4().hex
        folder = self.root / "voices" / voice_id
        folder.mkdir(parents=True)
        original = folder / ("original" + source.suffix.lower())
        shutil.copy2(source, original)
        sample = folder / "reference.wav"
        try:
            convert_audio(original, sample, start=health["selected_start"],
                          duration=health["selected_length"], clarity=clarity)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        voice = {
            "id": voice_id, "name": (name or source.stem).strip()[:100],
            "tags": tags.strip()[:250], "notes": notes.strip()[:1000], "favorite": False,
            "created": now(), "reference": str(sample.relative_to(self.root)),
            "original": str(original.relative_to(self.root)), "health": health,
            "settings": {}, "source_length": health["duration_seconds"],
            "model": MODEL_REPO, "model_revision": MODEL_REVISION,
        }
        self.voices.append(voice)
        self.save()
        return voice

    def update_voice(self, voice_id, **changes):
        voice = self.get_voice(voice_id)
        if not voice:
            raise KeyError(voice_id)
        for field in ("name", "tags", "notes", "favorite", "settings"):
            if field in changes:
                voice[field] = changes[field]
        self.save()
        return voice

    def duplicate(self, voice_id):
        voice = self.get_voice(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        clone = copy.deepcopy(voice)
        clone["id"] = uuid.uuid4().hex
        clone["name"] += " copy"
        clone["created"] = now()
        path = self.root / "voices" / clone["id"]
        path.mkdir(parents=True)
        for key in ("reference", "original"):
            if key in voice:
                old_path = self.root / voice[key]
                new_path = path / old_path.name
                shutil.copy2(old_path, new_path)
                clone[key] = str(new_path.relative_to(self.root))
        self.voices.append(clone)
        self.save()
        return clone

    def export_voice(self, voice_id, destination: Path):
        voice = self.get_voice(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as z:
            # A voice pack captures conditioning audio, original sample, metadata and settings.
            # Shared inference models are separately cached, NOT misleadingly embedded here.
            manifest = {k: v for k, v in voice.items() if k not in ("reference", "original")}
            manifest["format_version"] = FORMAT_VERSION
            manifest["model_assets_included"] = False
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.write(self.root / voice["reference"], "reference.wav")
            old = self.root / voice["original"]
            z.write(old, "original" + old.suffix)
        return destination

    def import_voice(self, package: Path, consent=False):
        if not consent:
            raise ValueError("Confirm permission before importing a voice.")
        with zipfile.ZipFile(package, "r") as z:
            names = z.namelist()
            if names.count("manifest.json") != 1 or names.count("reference.wav") != 1:
                raise ValueError("Missing or duplicate voice package contents")
            if len(names) > 4 or any(info.file_size > 300 * 1024 * 1024 for info in z.infolist()):
                raise ValueError("Voice package too large")
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format_version") != FORMAT_VERSION:
                raise ValueError("Unsupported voice package version")
            originals = [n for n in names if re.fullmatch(r"original\.[A-Za-z0-9]+", n)]
            voice_id = uuid.uuid4().hex
            folder = self.root / "voices" / voice_id
            folder.mkdir(parents=True)
            try:
                reference = folder / "reference.wav"
                reference.write_bytes(z.read("reference.wav"))
                if not 1 <= sf.info(str(reference)).duration <= 60:
                    raise ValueError("Package conditioning reference duration invalid")
                original = folder / (originals[0] if originals else "original.wav")
                original.write_bytes(z.read(originals[0]) if originals else reference.read_bytes())
                voice = {
                    "id": voice_id, "name": str(manifest.get("name", "Imported voice"))[:100],
                    "tags": str(manifest.get("tags", ""))[:250],
                    "notes": str(manifest.get("notes", ""))[:1000],
                    "favorite": bool(manifest.get("favorite", False)), "created": now(),
                    "reference": str(reference.relative_to(self.root)),
                    "original": str(original.relative_to(self.root)),
                    "health": manifest.get("health", {}), "settings": manifest.get("settings", {}),
                    "source_length": manifest.get("source_length", 0),
                    "model": MODEL_REPO, "model_revision": MODEL_REVISION,
                }
                self.voices.append(voice)
                self.save()
                return voice
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise

    def delete_voice(self, voice_id):
        voice = self.get_voice(voice_id)
        if voice:
            self.voices.remove(voice)
            shutil.rmtree(self.root / "voices" / voice_id, ignore_errors=True)
            self.save()

    def append_history(self, text, voice_id, audio, sample_rate):
        entry_id = uuid.uuid4().hex
        location = self.root / "history" / (entry_id + ".wav")
        sf.write(location, audio, int(sample_rate), subtype="PCM_16")
        self.history.insert(0, {
            "id": entry_id, "voice_id": voice_id, "text": text[:20000],
            "path": str(location.relative_to(self.root)), "created": now(),
        })
        limit = int(clamp(self.settings.get("history_limit", 50), 1, 500))
        for old in self.history[limit:]:
            (self.root / old["path"]).unlink(missing_ok=True)
        self.history = self.history[:limit]
        self.save()
        return self.history[0]


def replace_pronunciations(text, dictionary: dict):
    for term, replacement in sorted(dictionary.items(), key=lambda p: -len(p[0])):
        if term.strip() and isinstance(replacement, str):
            text = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", lambda _: replacement, text, flags=re.I)
    return text


def process_output(audio, sample_rate, speed=1.0, pitch=0.0, clarity=0):
    """Independent pitch and tempo using FFmpeg resampling and atempo."""
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(x):
        return x
    if speed == 1 and pitch == 0 and clarity == 0:
        return x
    pitch_ratio = 2 ** (float(pitch) / 12)
    tempo = float(speed) / pitch_ratio
    # speed [0.5,2] and pitch [-12,12] make 0.25..4; FFmpeg atempo supports [0.5,100].
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.wav"
        target = Path(tmp) / "out.wav"
        sf.write(source, x, sample_rate, subtype="PCM_16")
        filters = [
            f"asetrate={int(round(sample_rate * pitch_ratio))}",
            f"aresample={sample_rate}",
        ]
        while tempo < 0.5:
            filters.append("atempo=0.5")
            tempo /= .5
        while tempo > 2:
            filters.append("atempo=2.0")
            tempo /= 2
        filters.append(f"atempo={tempo:.6f}")
        if clarity:
            filters.append(f"afftdn=nr={int(5 + 14 * clamp(clarity, 0, 100) / 100)}")
        run_ffmpeg(["-i", source, "-af", ",".join(filters), "-ar", str(sample_rate), "-ac", "1", target])
        samples, _ = sf.read(target, dtype="float32", always_2d=False)
        return samples


def export_audio(audio, sample_rate, destination, channels=1, bitrate=192, target_rate=None):
    destination = Path(destination)
    if destination.suffix.lower() not in (".wav", ".mp3"):
        raise ValueError("Export must be WAV or MP3.")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "audio.wav"
        sf.write(source, np.asarray(audio, dtype=np.float32), sample_rate, subtype="PCM_24")
        rate = int(target_rate or sample_rate)
        args = ["-i", source, "-ar", str(rate), "-ac", str(int(channels))]
        if destination.suffix.lower() == ".mp3":
            args += ["-c:a", "libmp3lame", "-b:a", f"{int(bitrate)}k"]
        else:
            args += ["-c:a", "pcm_s24le"]
        run_ffmpeg([*args, destination])
    return destination


def blend_audio(primary, secondary, percent):
    percent = clamp(percent, 0, 100) / 100
    a, b = np.asarray(primary, dtype=np.float32), np.asarray(secondary, dtype=np.float32)
    length = max(len(a), len(b))
    mixed = np.zeros(length, dtype=np.float32)
    mixed[:len(a)] += (1 - percent) * a
    mixed[:len(b)] += percent * b
    return np.clip(mixed, -1, 1)


class PocketEngine:
    def __init__(self, library: Library):
        self.library = library
        self.tts = None
        self.last_key = None
        self.voice_cache = {}

    def load(self, settings, status=lambda _: None, stop=None):
        from pocket_tts_onnx import PocketTTSOnnx
        p = parameters(settings)
        lang = settings.get("language", "english_2026-04")
        key = (lang, p["steps"], p["temperature"])
        if key == self.last_key and self.tts:
            return self.tts
        stop = stop or Event()
        status("Checking pinned Pocket TTS ONNX weights…")
        models = ensure_public_models(self.library.root / "models", lang, status, stop)
        if stop.is_set():
            return None
        if models is None:
            raise RuntimeError("Model acquisition cancelled.")
        status("Loading Pocket TTS inference sessions…")
        self.tts = PocketTTSOnnx(models_dir=str(models), language=language_folder(lang),
                                 precision="int8", device="cpu",
                                 temperature=p["temperature"], lsd_steps=p["steps"])
        self.last_key = key
        self.voice_cache.clear()
        return self.tts

    def encode(self, voice, tts, status=lambda _: None):
        path = self.library.condition_path(voice)
        cache_key = (str(path), path.stat().st_mtime_ns, self.last_key)
        if cache_key not in self.voice_cache:
            status("Encoding local reference voice…")
            embedding = tts.encode_voice(path)
            if not np.asarray(embedding).size:
                raise RuntimeError("No usable voice embedding from reference.")
            self.voice_cache[cache_key] = embedding
        return self.voice_cache[cache_key]

    def synthesize(self, text: str, voice, settings, status=lambda _: None,
                   stop: Event | None = None, on_chunk: Callable | None = None):
        stop = stop or Event()
        text = replace_pronunciations(text.strip(), self.library.dictionary)
        if not text:
            raise ValueError("There is no text to read.")
        tts = self.load(settings, status, stop)
        if stop.is_set() or tts is None:
            return np.zeros(0, dtype=np.float32), 24000
        embedding = self.encode(voice, tts, status)
        all_chunks = []
        sentences = split_text(text, 220)
        for index, sentence in enumerate(sentences, 1):
            if stop.is_set():
                break
            status(f"Generating {index}/{len(sentences)}…")
            for samples in tts.stream(sentence, voice=embedding):
                if stop.is_set():
                    break
                block = np.asarray(samples, dtype=np.float32).reshape(-1)
                if block.size:
                    all_chunks.append(block)
                    if on_chunk:
                        on_chunk(block, int(tts.sample_rate))
        audio = np.concatenate(all_chunks) if all_chunks else np.zeros(0, dtype=np.float32)
        if not stop.is_set():
            p = parameters(settings)
            audio = process_output(audio, int(tts.sample_rate),
                                   speed=p["speed"], pitch=p["pitch"], clarity=p["clarity"])
        return audio, int(tts.sample_rate)


def transcribe_local(audio_file: Path, library: Library, status=lambda _: None):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("Install faster-whisper to enable microphone/audio speech conversion.") from e
    name = library.settings.get("transcription_model", "tiny")
    if name not in ("tiny", "base", "small"):
        name = "tiny"
    status(f"Loading local Whisper {name} (first run may download weights)…")
    whisper = WhisperModel(name, device="cpu", compute_type="int8",
                           download_root=str(library.root / "cache" / "whisper"))
    status("Transcribing speech locally…")
    segments, _ = whisper.transcribe(str(audio_file), vad_filter=True, beam_size=3)
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        raise ValueError("No intelligible speech detected in recording.")
    return text
