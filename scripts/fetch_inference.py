"""Download the same revision of public Pocket TTS ONNX code used by the working repo.

Weights are not embedded and download to local VoiceData on first use.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.request import urlopen

REVISION = "58a6d00cf13d239b6748cb0769f35c580a8f606c"
URL = f"https://huggingface.co/KevinAHM/pocket-tts-onnx/resolve/{REVISION}/pocket_tts_onnx.py"


def main():
    target = Path(__file__).resolve().parents[1] / "pocket_tts_onnx.py"
    if target.is_file():
        print("ONNX source already present:", target)
        return
    with urlopen(URL, timeout=90) as response:
        data = response.read(1024 * 1024)
    if len(data) < 10000 or b"class PocketTTSOnnx" not in data or b"def stream" not in data:
        raise RuntimeError("Downloaded pinned ONNX source is incomplete or unexpected.")
    target.write_bytes(data)
    print("Fetched pinned PocketTTSOnnx module:", REVISION, "sha256:", hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    main()
