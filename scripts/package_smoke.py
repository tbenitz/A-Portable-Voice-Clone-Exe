"""Build representative personalized single-voice EXE to exercise actual Windows loader."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import soundfile as sf

from studio.single_export import generate_single_exe, verify_template


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    base = verify_template(args.template)
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "test.wav"
        # Test-only voice clip; waveform validity, NOT similarity to any real person.
        time = np.arange(24000 * 5, dtype=np.float32) / 24000
        sf.write(source, (.14 * np.sin(time * 2 * np.pi * 180)).astype("float32"), 24000)
        personalized = generate_single_exe(base, source.read_bytes(), "Windows smoke", Path(args.output).resolve())
        run = subprocess.run([str(personalized), "--self-test"], timeout=40)
        if run.returncode:
            raise RuntimeError(f"Personalized Windows PE self-test returned {run.returncode}")
        print("PASS: appended voice profile, native Windows PE loader, complete runtime ZIP trailer.")


if __name__ == "__main__":
    main()
