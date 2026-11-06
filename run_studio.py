"""Entry point for source or PyInstaller --windowed portable packaging."""
import sys
import traceback

from studio.app import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        from studio.core import data_home
        log = data_home() / "last-error.txt"
        details = traceback.format_exc()
        log.write_text(details, encoding="utf-8")
        if getattr(sys, "frozen", False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0,
                f"The voice studio could not start.\n\n{details[-350:]}\n\nFull error: {log}",
                "Voice Clone Studio - startup error", 0x10)
            sys.exit(1)
        raise
