# Portable Voice Clone Studio

A Windows desktop voice library, text-to-speech studio and microphone transcription-to-cloned-voice workflow powered by **Pocket TTS ONNX**. Based on the working Pocket engine from [supertonic3gpt-clone](https://github.com/tbenitz/supertonic3gpt-clone).

This is a desktop project, not the browser demo or a fake voice simulator. The actual generator is KevinAHM's PocketTTSOnnx inference code, pinned to the revision used by the working project.

**Status:** Source and Windows build CI. Download a Windows build from Actions → Build portable studio → Artifacts once it passes. End-to-end audio, microphone and hotkey tests must be checked on a real Windows PC.

## What it does
- Voice library: import/record long references, analyze and clean audio, store original and chosen **short** conditioning segment, preview, favorite, tag, duplicate, export/import `.voicepack` ZIP.
- Offline-first after initial model and optional speech-recognition downloads; local data is stored beside the EXE in writable `VoiceData`, else under local user data. No cloud voice uploads.
- Pocket ONNX TTS with speed/independent pitch postprocessing, temperature (expressiveness), inference steps, batch generation, WAV/MP3 output, history, saved profiles and a pronunciation-replacement dictionary.
- Floating always-on-top mini controller, system tray when available, and configurable global recording hotkey.
- Mic and audio-file speech-to-speech **via local ASR → Pocket TTS** (not zero-delay voice conversion); choose a virtual audio output device for OBS/Discord routing.
- A/B text generation and experimental two-voice blend.

## Known model and hardware limitations
Pocket TTS works from a relatively short conditioning sample; selecting 30 minutes does **not** train or fine-tune the underlying model. Longer recordings are stored and scanned for an intelligible segment. Reference health is a signal-quality estimate, not an identity-match guarantee. High/Ultra use more flow steps and may be slower but are not automatically better on all voices. No guaranteed emotions/style tokens, no true simultaneous speech-to-speech; mic is transcribed when released (requires a downloaded faster-whisper model). Pitch/speed and MP3 export use bundled FFmpeg, so postprocessed output may take longer to begin playing. The bundled ONNX models are INT8; we do not label them FP32.

## Build
GitHub Actions builds a portable **folder** containing `VoiceCloneStudio.exe` (not a misleading single-file EXE that secretly re-downloads runtime libraries). Download/extract the artifact ZIP and keep the executable beside its `_internal` folder. VoiceData lives beside the EXE if writable. Start the EXE; its first synthesis downloads the pinned public model bundle. Optional speech recognition downloads the open-source Whisper tiny model once. All generated audio and source references remain local.

For local Python development: `python -m pip install -r requirements.txt`; then download the pinned ONNX inference module via `scripts/fetch_inference.py`; run `python -m studio.app`. To run pure logic tests: `python -m unittest discover -s tests -v`.

## Privacy and attribution
Only clone voices you own or are authorized to use. Never present synthesized speech deceptively as a real recording. Models and third-party libraries remain subject to their own licenses. Core voice inference: [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) and [KevinAHM's ONNX conversion](https://huggingface.co/KevinAHM/pocket-tts-onnx). First-run model download may be required; after model caching the software does not need a network connection.
