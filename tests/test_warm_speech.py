import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from studio.warm_speech import LatestRequest, WarmVoice, decode_request


class WarmSpeechTests(unittest.TestCase):
    def test_wire_protocol_and_auth(self):
        token = "A" * 64
        profile = b'{"engine":"pocket"}'
        frame = token.encode() + b"\nspeak\n" + struct.pack("<I", len(profile)) + profile + "Hello 🌎".encode()
        self.assertEqual(decode_request(frame, token), ("speak", profile, "Hello 🌎"))
        with self.assertRaises(ValueError):
            decode_request(frame, "B" * 64)

    def test_pending_is_latest_not_a_backlog(self):
        mailbox = LatestRequest()
        a, b, c = [("speak", b"profile", word) for word in ("one", "two", "three")]
        self.assertEqual(mailbox.submit(a), b"A")
        self.assertEqual(mailbox.take(), a)
        self.assertEqual(mailbox.submit(a), b"D")
        mailbox.submit(b)
        mailbox.submit(c)
        mailbox.done()
        self.assertEqual(mailbox.take(), c)
        mailbox.done()
        self.assertEqual(mailbox.submit(c), b"A")
        self.assertEqual(mailbox.take(), c)
        mailbox.submit(("stop", b"", ""))
        mailbox.done()
        self.assertIsNone(mailbox.take())

    def test_reuses_engine_and_voice_path_between_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = MagicMock()
            engine.voice_cache = {}
            engine.synthesize.return_value = ([0.0, 0.1], 24000)
            playback = MagicMock()
            profile = json.dumps({"engine":"pocket", "voice_wav_b64":
                base64.b64encode(b"RIFF" + b"0000" + b"WAVE" + b"0"*40).decode()}).encode()
            with patch("studio.warm_speech.PocketEngine", return_value=engine) as factory:
                voice = WarmVoice(object(), folder, playback)
                voice.handle(("speak", profile, "first"))
                path = Path(voice.voice["reference"])
                stamp = path.stat().st_mtime_ns
                voice.handle(("speak", profile, "second"))
                self.assertEqual(path.stat().st_mtime_ns, stamp)
                factory.assert_called_once()
                self.assertEqual([c.args[0] for c in engine.synthesize.call_args_list], ["first", "second"])
                self.assertEqual(playback.call_count, 2)
                voice.handle(("warmup", profile, ""))
                engine.load.assert_called_once()
                engine.encode.assert_called_once()
                self.assertEqual(playback.call_count, 2)


if __name__ == "__main__":
    unittest.main()
