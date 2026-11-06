"""Frozen single-voice runner, preserving a visible error dialog if startup fails."""
import sys
import traceback
from studio.single_voice import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if getattr(sys, "frozen", False):
            import ctypes
            import os
            from pathlib import Path
            folder = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PortableVoiceClone"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "single-startup-error.txt"
            path.write_text(traceback.format_exc(), encoding="utf-8")
            ctypes.windll.user32.MessageBoxW(0, "Pocket single-voice UI failed.\n\nError log: " + str(path),
                                             "Pocket Voice - Startup", 0x10)
            sys.exit(1)
        raise
