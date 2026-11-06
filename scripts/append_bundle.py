"""Attach the frozen Python runtime ZIP to the .NET NativeAOT single EXE.

File layout:
[Native EXE][PKZIP frozen runtime][CVRZIP01][8-byte LE ZIP length]
Then the user's browser or the desktop studio appends:
[JSON voice profile][CVRCFG01][8-byte LE JSON length]
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path


def build(launcher: Path, zipped: Path, output: Path):
    with launcher.open("rb") as file:
        if file.read(2) != b"MZ":
            raise ValueError("Launcher must be a Windows native PE")
    with zipped.open("rb") as file:
        if file.read(2) != b"PK":
            raise ValueError("Runtime must be a ZIP archive")
    if zipped.stat().st_size < 1_000_000:
        raise ValueError("Runtime archive too small")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as result, launcher.open("rb") as a, zipped.open("rb") as b:
        while chunk := a.read(1024*1024):
            result.write(chunk)
        while chunk := b.read(1024*1024):
            result.write(chunk)
        result.write(b"CVRZIP01")
        result.write(struct.pack("<Q", zipped.stat().st_size))
    return output


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Usage: append_bundle.py launcher.exe runtime.zip output.exe")
    output = build(*map(Path, sys.argv[1:]))
    print(f"Bundled {output} ({output.stat().st_size:,} bytes)")
