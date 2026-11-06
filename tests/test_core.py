"""PortableVoiceClone regression tests: real local files, mocked ONNX inference.

The optional GitHub Actions 'speech_smoke' test exercises downloaded model weights.
UI microphone/device testing requires a physical Windows audio device.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

from studio import core


def voice_sample(path, seconds=4, rate=16000, amplitude=.12):
    t = np.arange(int(seconds * rate)) / rate
    # Synthetic voiced-signal-like reference for file IO only; never used to judge actual voice quality.
    signal = (amplitude * np.sin(2 * np.pi * 180 * t) +
              .35 * amplitude * np.sin(2 * np.pi * 365 * t)).astype("float32")
    sf.write(path, signal, rate)
    return path


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib = core.Library(self.root / "data")
        self.sample = voice_sample(self.root / "sample.wav")

    def tearDown(self):
        self.tmp.cleanup()

    def test_presets_are_real_step_settings(self):
        for value, steps in (("Real-time", 1), ("Balanced", 2),
                             ("High quality", 4), ("Ultra", 8)):
            self.assertEqual(core.parameters(dict(self.lib.settings, preset=value))["steps"], steps)

    def test_stability_changes_onnx_temperature(self):
        low = core.parameters(dict(self.lib.settings, stability=0))["temperature"]
        high = core.parameters(dict(self.lib.settings, stability=100))["temperature"]
        self.assertGreater(low, high)

    def test_safe_pitch_clamp(self):
        self.assertEqual(core.parameters(dict(self.lib.settings, pitch=12))["pitch"], 8)
        self.assertEqual(core.parameters(dict(self.lib.settings, pitch=-12, safe_mode=False))["pitch"], -12)

    def test_metrics_are_not_fake_pass_for_silence(self):
        self.assertLess(core.signal_metrics(np.zeros(16000))["score"], 30)
        self.assertIn("quiet", core.signal_metrics(np.zeros(16000))["hint"].lower())

    def test_clipping_detected(self):
        result = core.signal_metrics(np.ones(16000, dtype="float32"))
        self.assertGreater(result["clipping"], .9)
        self.assertIn("clipping", result["hint"].lower())

    def test_long_audio_window_and_duration(self):
        rate = 16000
        quiet = np.zeros(rate * 18, dtype="float32")
        active = np.full(rate * 18, .11, dtype="float32")
        longref = self.root / "long.wav"
        sf.write(longref, np.concatenate((quiet, active)), rate)
        health = core.inspect_reference(longref)
        self.assertAlmostEqual(health["duration_seconds"], 36, places=1)
        self.assertGreaterEqual(health["selected_start"], 12)
        self.assertLessEqual(health["selected_length"], 18)

    def test_import_requires_permission(self):
        with self.assertRaises(ValueError):
            self.lib.add_voice(self.sample, "Not authorized")

    def test_library_add_voice_and_persistence(self):
        voice = self.lib.add_voice(self.sample, "My voice", tags="lecture", consent=True)
        self.assertEqual(voice["name"], "My voice")
        self.assertTrue(self.lib.condition_path(voice).is_file())
        self.assertLessEqual(sf.info(str(self.lib.condition_path(voice))).duration, 20)
        self.assertTrue((self.lib.root / voice["original"]).is_file())
        fresh = core.Library(self.lib.root)
        self.assertEqual(fresh.get_voice(voice["id"])["tags"], "lecture")

    def test_duplicate_not_overwrite_original(self):
        voice = self.lib.add_voice(self.sample, "Original", consent=True)
        duplicated = self.lib.duplicate(voice["id"])
        self.lib.update_voice(duplicated["id"], name="Edited", tags="variant")
        self.assertEqual(self.lib.get_voice(voice["id"])["name"], "Original")
        self.assertNotEqual(duplicated["reference"], voice["reference"])
        self.assertEqual(self.lib.get_voice(duplicated["id"])["tags"], "variant")

    def test_export_import_voice_package(self):
        original = self.lib.add_voice(self.sample, "Pack-test", consent=True)
        package = self.root / "voice.voicepack"
        self.lib.export_voice(original["id"], package)
        with zipfile.ZipFile(package) as z:
            manifest = json.loads(z.read("manifest.json"))
            self.assertFalse(manifest["model_assets_included"])
            self.assertIn("reference.wav", z.namelist())
        restored = self.lib.import_voice(package, consent=True)
        self.assertNotEqual(restored["id"], original["id"])
        self.assertEqual(restored["name"], original["name"])
        self.assertTrue(self.lib.condition_path(restored).is_file())

    def test_package_import_requires_permission(self):
        package = self.root / "unauthorized.voicepack"
        with self.assertRaises(ValueError):
            self.lib.import_voice(package, consent=False)

    def test_package_rejects_missing_reference(self):
        archive = self.root / "broken.voicepack"
        with zipfile.ZipFile(archive, "w") as zipout:
            zipout.writestr("manifest.json", json.dumps({"format_version": 1}))
        with self.assertRaises(ValueError):
            self.lib.import_voice(archive, consent=True)

    def test_dictionary_escapes_regex_meta(self):
        result = core.replace_pronunciations("C++ and Mr. JONES", {"C++": "see plus plus", "Mr. Jones": "mister jones"})
        self.assertEqual(result, "see plus plus and mister jones")

    def test_blend_percentages(self):
        a = np.ones(50, dtype="float32")
        b = np.zeros(80, dtype="float32")
        self.assertEqual(len(core.blend_audio(a, b, 50)), 80)
        self.assertAlmostEqual(core.blend_audio(a, b, 50)[0], .5)
        self.assertAlmostEqual(core.blend_audio(a, b, 100)[0], 0)

    def test_audio_pitch_tempo_postprocessor(self):
        signal = np.sin(np.arange(24000) * 2 * np.pi * 220 / 24000).astype("float32")
        faster = core.process_output(signal, 24000, speed=2)
        slower = core.process_output(signal, 24000, speed=.5)
        self.assertLess(len(faster), 18000)
        self.assertGreater(len(slower), 36000)
        shifted = core.process_output(signal, 24000, speed=1, pitch=4)
        self.assertGreater(len(shifted), 20000)

    def test_wav_and_mp3_exports(self):
        samples = np.sin(np.arange(24000) * 2 * np.pi * 220 / 24000).astype("float32")
        wav = core.export_audio(samples, 24000, self.root / "out.wav", channels=2, target_rate=48000)
        self.assertEqual(sf.info(wav).channels, 2)
        self.assertEqual(sf.info(wav).samplerate, 48000)
        mp3 = core.export_audio(samples, 24000, self.root / "out.mp3", channels=1, bitrate=128)
        self.assertGreater(mp3.stat().st_size, 1000)

    def test_history_limit_cleans_old_files(self):
        self.lib.settings["history_limit"] = 2
        ids = [self.lib.append_history(str(i), "voice", np.ones(100, dtype="float32"), 24000) for i in range(3)]
        self.assertEqual(len(self.lib.history), 2)
        self.assertFalse((self.lib.root / ids[0]["path"]).exists())

    def test_stop_before_model_loading(self):
        voice = self.lib.add_voice(self.sample, "Clone", consent=True)
        stop = Event()
        stop.set()
        engine = core.PocketEngine(self.lib)
        with patch.dict(sys.modules, {"pocket_tts_onnx": SimpleNamespace(PocketTTSOnnx=object)}):
            with patch("studio.core.ensure_public_models", return_value=None):
                sound, rate = engine.synthesize("Hello", voice, self.lib.settings, stop=stop)
        self.assertFalse(len(sound))
        self.assertEqual(rate, 24000)

    def test_real_engine_call_path_with_mocked_onnx(self):
        voice = self.lib.add_voice(self.sample, "Clone", consent=True)
        chunks = []
        class FakePocket:
            def __init__(self, **kwargs):
                self.sample_rate = 24000
                self.parameters = kwargs
            def encode_voice(self, path):
                self.path = str(path)
                return np.ones((1, 6), dtype="float32")
            def stream(self, text, voice):
                self.last_text = text
                yield np.full(480, .1, dtype="float32")
                yield np.full(480, .2, dtype="float32")
        engine = core.PocketEngine(self.lib)
        config = dict(self.lib.settings, speed=1, pitch=0, clarity=0)
        with patch.dict(sys.modules, {"pocket_tts_onnx": SimpleNamespace(PocketTTSOnnx=FakePocket)}):
            with patch("studio.core.ensure_public_models", return_value=self.root / "fake-models"):
                sound, rate = engine.synthesize("Hi there.", voice, config,
                                   on_chunk=lambda audio, hz: chunks.append((len(audio), hz)))
        self.assertEqual(rate, 24000)
        self.assertEqual(len(sound), 960)
        self.assertEqual(chunks, [(480, 24000), (480, 24000)])
        self.assertEqual(engine.tts.parameters["precision"], "int8")
        self.assertTrue(engine.tts.path.endswith("reference.wav"))

    def test_cli_parser_has_real_integration_hooks(self):
        from studio.app import main
        with patch("studio.app.cli_main", return_value=0) as cli:
            main(["--self-test"])
            cli.assert_called_once()

    def test_source_uses_verified_working_pocket_provider(self):
        text = Path(core.__file__).read_text(encoding="utf-8")
        self.assertIn("from pocket_tts_onnx import PocketTTSOnnx", text)
        self.assertIn("ensure_public_models", text)
        self.assertIn("tts.stream", text)


if __name__ == "__main__":
    unittest.main()
