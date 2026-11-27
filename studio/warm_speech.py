"""Hidden persistent clipboard speech worker, Windows local named-pipe protocol."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import tempfile
import threading
from pathlib import Path
from multiprocessing.connection import Listener

from .core import Library, PocketEngine

MAX_PROFILE = 8_000_000
MAX_TEXT = 262_144


def decode_request(raw, token):
    supplied, sep, tail = raw.partition(b"\n")
    if not sep or not hmac.compare_digest(supplied, token.encode("ascii")):
        raise ValueError("Invalid request authentication")
    command, sep, tail = tail.partition(b"\n")
    if not sep or command not in (b"speak", b"warmup", b"stop") or len(tail) < 4:
        raise ValueError("Invalid request")
    size = struct.unpack_from("<I", tail)[0]
    if size > MAX_PROFILE or len(tail) < 4 + size or len(tail) - 4 - size > MAX_TEXT:
        raise ValueError("Invalid request length")
    profile, phrase = tail[4:4+size], tail[4+size:].decode("utf-8")
    if command != b"stop" and not profile:
        raise ValueError("Missing voice")
    if command == b"speak" and not phrase.strip():
        raise ValueError("Empty speech")
    return command.decode("ascii"), profile, phrase


class LatestRequest:
    """One active request and at most one newest pending request; no old queue."""
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = None
        self.active = None
        self.stopping = False

    def submit(self, request):
        with self.condition:
            if request[0] == "stop":
                self.stopping = True
                self.pending = None
                self.condition.notify_all()
                return b"A"
            if self.stopping:
                return b"D"
            if request == self.active or request == self.pending:
                return b"D"
            if request[0] == "warmup" and (self.active or self.pending):
                return b"D"
            self.pending = request
            self.condition.notify_all()
            return b"A"

    def take(self):
        with self.condition:
            self.condition.wait_for(lambda: self.pending is not None or self.stopping)
            if self.stopping:
                return None
            self.active, self.pending = self.pending, None
            return self.active

    def done(self):
        with self.condition:
            self.active = None


class WarmVoice:
    def __init__(self, library, directory, playback):
        self.engine = PocketEngine(library)
        self.directory = Path(directory)
        self.playback = playback
        self.key = None
        self.voice = None
        self.settings = None

    def prepare(self, profile_bytes):
        key = hashlib.sha256(profile_bytes).digest()
        if key == self.key:
            return
        data = json.loads(profile_bytes)
        if data.get("engine") != "pocket":
            raise ValueError("Unsupported speech engine")
        lang = data.get("lang", "english_2026-04")
        if lang not in {"english_2026-04", "english", "german", "spanish", "italian", "portuguese"}:
            raise ValueError("Unsupported voice language")
        wav = base64.b64decode(data["voice_wav_b64"], validate=True)
        if not 44 <= len(wav) <= 5_000_000 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise ValueError("Invalid reference recording")
        path = self.directory / "reference.wav"
        path.write_bytes(wav)
        self.engine.voice_cache.clear()
        self.voice = {"id": "personalized", "name": str(data.get("voice_name", "My voice"))[:72],
                      "reference": str(path)}
        self.settings = {"preset": "Real-time", "speed": 1.0, "pitch": 0.0,
                         "stability": 50, "clarity": 0, "language": lang, "safe_mode": True}
        self.key = key

    def handle(self, request):
        command, profile, phrase = request
        self.prepare(profile)
        if command == "warmup":
            tts = self.engine.load(self.settings)
            self.engine.encode(self.voice, tts)
            return
        audio, rate = self.engine.synthesize(phrase, self.voice, self.settings)
        self.playback(audio, rate)


def run_worker(state_path):
    from .single_voice import play_buffered_audio
    state_path = Path(state_path)
    pipe, token = state_path.read_text(encoding="utf-8").splitlines()[:2]
    base = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PortableVoiceClone" / "VoiceData"
    base.mkdir(parents=True, exist_ok=True)
    mailbox = LatestRequest()
    work = Path(tempfile.mkdtemp(prefix="warm-voice-", dir=state_path.parent))
    listener = None

    def serve():
        try:
            while not mailbox.stopping:
                try:
                    with listener.accept() as conn:
                        if not conn.poll(3):
                            continue
                        raw = conn.recv_bytes(MAX_PROFILE + MAX_TEXT + 128)
                        try:
                            req = decode_request(raw, token)
                            result = mailbox.submit(req)
                        except (ValueError, UnicodeError, struct.error):
                            result = b"E"
                        conn.send_bytes(result)
                except (OSError, EOFError):
                    if mailbox.stopping:
                        break
        finally:
            mailbox.submit(("stop", b"", ""))

    try:
        listener = Listener("\\\\.\\pipe\\" + pipe, family="AF_PIPE")
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        voice = WarmVoice(Library(base), work, play_buffered_audio)
        while True:
            req = mailbox.take()
            if req is None:
                break
            try:
                voice.handle(req)
            except Exception as exc:
                # Do not log copied text or the voice profile.
                (base / "warm-speech-error.txt").write_text(type(exc).__name__, encoding="utf-8")
            finally:
                mailbox.done()
        # Ensure the stop acknowledgment reaches the launcher before exit.
        thread.join(timeout=5)
        return 0
    finally:
        if listener is not None:
            listener.close()
        shutil.rmtree(work, ignore_errors=True)
        try:
            if state_path.read_text(encoding="utf-8").splitlines()[1] == token:
                state_path.unlink()
        except (OSError, IndexError):
            pass
