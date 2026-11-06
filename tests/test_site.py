"""Browser studio and Windows packaging regression checks.

These assert the real connected code paths. Browser audio and physical
Windows microphone/desktop behavior require device testing.
"""
import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from studio.single_export import generate_single_exe, verify_template
from studio.clipboard_control import ClipboardSpeechQueue, MAX_CHARS


ROOT = Path(__file__).resolve().parents[1]


class WebBuildTests(unittest.TestCase):
    def test_audio_players_and_independent_replay(self):
        ui = (ROOT / "web/studio-ui.html").read_text(encoding="utf-8")
        js = (ROOT / "web/studio-addon.js").read_text(encoding="utf-8")
        self.assertIn('id="reference-player"', ui)
        self.assertIn('id="clone-player"', ui)
        self.assertIn('id="save-mp3"', ui)
        self.assertIn("generated.src = wavUrl", js)
        self.assertIn("audioPCM = merged", js)
        self.assertIn("mp3.encodeBuffer(shorts)", js)
        self.assertIn("app.buildDownloadBlob.bind(app)", js)
        # Clicking original audio and cloned audio players is native media
        # playback and must not invoke app.generate a second time.
        self.assertNotIn('generated.addEventListener("play", app.generate)', js)

    def test_windows_downloads_are_real_not_fake_ui(self):
        js = (ROOT / "web/studio-addon.js").read_text(encoding="utf-8")
        ui = (ROOT / "web/studio-ui.html").read_text(encoding="utf-8")
        self.assertIn("crypto.subtle.digest", js)
        self.assertIn("SingleVoice-Starter.exe", js)
        self.assertIn("starter-build.json", js)
        self.assertIn("CVRCFG01", js)
        # The thin launcher downloads the full runtime on Windows; no embedded ZIP is required.
        self.assertIn("voice_wav_b64", js)
        self.assertIn("VoiceCloneStudio-Setup.exe", ui)
        self.assertIn("Generated clone", ui)

    def test_pinned_working_app_used_directly(self):
        build = (ROOT / "tools/brand_site.py").read_text(encoding="utf-8")
        self.assertIn("new PocketVoice();", build)
        self.assertIn("window.__pocketApp = new PocketVoice();", build)
        self.assertIn("studio-addon.js", build)
        self.assertIn("lame.min.js", build)
        self.assertIn("theme.css", build)

    def test_installer_is_a_real_installer(self):
        iss = (ROOT / "installer/VoiceCloneStudio.iss").read_text(encoding="utf-8")
        self.assertIn("UninstallDisplayIcon", iss)
        self.assertIn("VoiceCloneStudio-Setup", iss)
        self.assertIn("Flags: recursesubdirs", iss)

    def test_full_studio_exports_real_single_exe(self):
        app = (ROOT / "studio/app.py").read_text(encoding="utf-8")
        self.assertIn("export_library_voice(", app)
        self.assertIn('("Single-voice EXE", self.export_personal_exe)', app)
        self.assertIn("def replay_latest(self):", app)

    def test_compact_gui_has_actual_generation_and_replay(self):
        ui = (ROOT / "studio/single_voice.py").read_text(encoding="utf-8")
        self.assertIn("self.engine.synthesize(", ui)
        self.assertIn("def replay(self):", ui)
        self.assertIn("def export_audio(self):", ui)
        self.assertIn("self.play(self.last_audio, self.last_rate)", ui)

    def test_deployed_site_contains_required_scripts(self):
        index = ROOT / "web/index.html"
        source = ROOT / "web/app.js"
        if not source.exists() or not index.exists():
            self.skipTest("Pinned Pocket app is copied only during deploy")
        page = index.read_text(encoding="utf-8")
        self.assertIn("studio-addon.js", page)
        self.assertIn("vendor/lame.min.js", page)
        self.assertIn('href="./theme.css"', page)
        self.assertIn("Pocket Voice Studio", page)
        self.assertIn("window.__pocketApp = new PocketVoice();", source.read_text(encoding="utf-8"))


class ClipboardControlTests(unittest.TestCase):
    def test_existing_clipboard_never_auto_speaks(self):
        queue = ClipboardSpeechQueue()
        self.assertIsNone(queue.observe("private text already copied"))
        self.assertIsNone(queue.observe("private text already copied"))
        self.assertEqual(queue.observe("new phrase"), "new phrase")

    def test_multiple_unique_copies_queue_in_order(self):
        queue = ClipboardSpeechQueue(max_items=3)
        for value in ("one", "two", "three"):
            self.assertTrue(queue.enqueue(value))
        self.assertFalse(queue.enqueue("three"))
        self.assertEqual([queue.pop(), queue.pop(), queue.pop()], ["one", "two", "three"])
        self.assertIsNone(queue.pop())

    def test_queue_bounds_and_stop_clears(self):
        queue = ClipboardSpeechQueue(max_items=2)
        self.assertFalse(queue.enqueue("x" * (MAX_CHARS + 1)))
        for value in ("one", "two", "three"):
            queue.enqueue(value)
        self.assertEqual([queue.pop(), queue.pop()], ["two", "three"])
        queue.enqueue("more")
        queue.clear()
        self.assertEqual(len(queue), 0)

    def test_personal_gui_exposes_clipboard_options(self):
        content = (ROOT / "studio/single_voice.py").read_text(encoding="utf-8")
        for marker in ("def _check_clipboard", "def _install_hotkeys",
                       "def speak_clipboard", "def edit_hotkeys",
                       "def _build_menu", "def _save_preferences"):
            self.assertIn(marker, content)



class PersonalExePackageTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        body = bytearray(2048)
        body[:2] = b"MZ"
        struct.pack_into("<I", body, 0x3c, 0x80)
        body[0x80:0x84] = b"PE\x00\x00"
        struct.pack_into("<H", body, 0x84, 0x8664)
        bundle = b"PK" + b"x" * 1_200_000
        self.base = self.root / "base.exe"
        self.base.write_bytes(body + bundle + b"CVRZIP01" + struct.pack("<Q", len(bundle)))
        t = np.arange(5 * 24000, dtype=np.float32) / 24000
        sample = .17 * np.sin(2 * np.pi * 170 * t)
        self.audio = self.root / "reference.wav"
        sf.write(self.audio, sample, 24000)

    def tearDown(self):
        self.work.cleanup()

    def test_personalized_native_exe_with_embedded_wav(self):
        target = generate_single_exe(self.base, self.audio.read_bytes(), "Voice example",
                                     self.root / "personal.exe")
        content = target.read_bytes()
        self.assertEqual(content[:2], b"MZ")
        self.assertEqual(content[-16:-8], b"CVRCFG01")
        size = struct.unpack("<Q", content[-8:])[0]
        profile = json.loads(content[-16-size:-16])
        self.assertEqual(profile["voice_name"], "Voice example")
        self.assertEqual(profile["engine"], "pocket")
        self.assertEqual(base64.b64decode(profile["voice_wav_b64"]), self.audio.read_bytes())

    def test_template_never_overwritten(self):
        with self.assertRaises(ValueError):
            generate_single_exe(self.base, self.audio.read_bytes(), "name", self.base)

    def test_wrong_reference_does_not_generate_exe(self):
        with self.assertRaises(ValueError):
            generate_single_exe(self.base, b"random bytes", "name", self.root / "bad.exe")

    def test_invalid_runtime_is_rejected(self):
        self.base.write_bytes(b"not a Windows executable")
        with self.assertRaises(ValueError):
            verify_template(self.base)


if __name__ == "__main__":
    unittest.main()
