"""Optional CI model smoke test: real reference encoding and generated audio.

Requires network at initial model download; uploads the output WAV as artifact.
"""
from pathlib import Path
from threading import Event

import numpy as np
import soundfile as sf
from huggingface_hub import hf_hub_download

from studio.core import Library, PocketEngine

def main():
    root = Path("smoke-test-data").resolve()
    library = Library(root)
    original = hf_hub_download(repo_id="KevinAHM/pocket-tts-onnx",
                               filename="reference_sample.wav", token=False)
    # Use a real supplied voice sample rather than a synthetic tone.
    voice = library.add_voice(original, "Pocket smoke voice", consent=True)
    engine = PocketEngine(library)
    config = dict(library.settings, preset="Real-time")
    sound, rate = engine.synthesize(
        "Hello, this is a real Pocket TTS clone and local speech-generation test.",
        voice, config, status=print, stop=Event())
    if not len(sound) or not np.isfinite(sound).all() or len(sound) < rate // 3:
        raise RuntimeError("Pocket ONNX did not produce a valid speech waveform.")
    sf.write("pocket-smoke-output.wav", sound, rate)
    print(f"PASS: {len(sound)/rate:.2f}s of synthesized audio; reference signal score={voice['health']['score']}")

if __name__ == "__main__":
    main()
