"""Append a locally permitted Pocket voice to the verified Windows template.

Uses the same ZIP and profile trailers as the working clipboard EXE.
Never uploads voice PCM, even when called from the full studio.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

from .core import Library

PROFILE_MARKER = b"CVRCFG01"
BUNDLE_MARKER = b"CVRZIP01"


def make_profile(reference_wav: bytes, name: str, lang="english_2026-04"):
    if not 44 <= len(reference_wav) <= 5_000_000:
        raise ValueError("The selected voice sample is too large (max 5 MB WAV).")
    if reference_wav[:4] != b"RIFF" or reference_wav[8:12] != b"WAVE":
        raise ValueError("Reference is not a standard RIFF/WAV recording.")
    if lang not in {"english_2026-04", "english", "german", "italian", "spanish", "portuguese"}:
        raise ValueError("Unsupported Pocket TTS language")
    return json.dumps({
        "version": 1,
        "engine": "pocket",
        "voice_name": (name or "My Voice").strip()[:72],
        "lang": lang,
        "voice_wav_b64": base64.b64encode(reference_wav).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")


def verify_template(path):
    p = Path(path)
    with p.open("rb") as f:
        f.seek(0, 2)
        length = f.tell()
        if length <= 1_000_000:
            raise ValueError("Single-voice template executable too small.")
        f.seek(0)
        if f.read(2) != b"MZ":
            raise ValueError("Single-voice template lacks Windows EXE header.")
        f.seek(0x3c)
        pe_at = struct.unpack("<I", f.read(4))[0]
        if pe_at < 64 or pe_at > length - 100:
            raise ValueError("EXE PE header offset invalid.")
        f.seek(pe_at)
        if f.read(4) != b"PE\x00\x00" or struct.unpack("<H", f.read(2))[0] != 0x8664:
            raise ValueError("Template is not a Windows x64 executable.")
        f.seek(length - 16)
        tail = f.read(16)
        if tail[:8] != BUNDLE_MARKER:
            raise ValueError("Template does not contain the expected voice engine bundle.")
        size = struct.unpack("<Q", tail[8:])[0]
        if size < 1_000_000 or size > length - 2048:
            raise ValueError("Engine bundle length is invalid.")
        f.seek(length - 16 - size)
        if f.read(2) != b"PK":
            raise ValueError("Engine bundle ZIP header is invalid.")
    return p


def generate_single_exe(base_template, reference_wav, name, output_path, lang="english_2026-04", expected_sha256=None):
    base = verify_template(base_template)
    if expected_sha256:
        digest = hashlib.sha256(base.read_bytes()).hexdigest()
        if digest.lower() != expected_sha256.lower():
            raise ValueError("Template SHA-256 checksum mismatch")
    payload = make_profile(reference_wav, name, lang)
    if len(payload) > 8_000_000:
        raise ValueError("Voice profile exceeds 8 MB.")
    target = Path(output_path)
    if base.resolve() == target.resolve():
        raise ValueError("Cannot replace the shared base executable.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with base.open("rb") as source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
        destination.write(payload)
        destination.write(PROFILE_MARKER)
        destination.write(struct.pack("<Q", len(payload)))
    return target


def export_library_voice(lib: Library, voice_id: str, base_template, destination,
                         lang="english_2026-04"):
    voice = lib.get_voice(voice_id)
    if not voice:
        raise KeyError("Unknown voice")
    wav = lib.condition_path(voice).read_bytes()
    return generate_single_exe(base_template, wav, voice["name"], destination, lang)


def locate_template():
    """Find template installed alongside the full suite, or in source checkout."""
    import sys
    options = []
    if getattr(sys, "frozen", False):
        directory = Path(sys.executable).resolve().parent
        options.extend([directory / "resources" / "SingleVoice-Base.exe",
                        directory / "_internal" / "resources" / "SingleVoice-Base.exe"])
    directory = Path(__file__).resolve().parent.parent
    options.extend([directory / "web" / "download" / "SingleVoice-Base.exe",
                    directory / "resources" / "SingleVoice-Base.exe"])
    return next((p for p in options if p.is_file()), None)
