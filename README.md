# Pocket Voice Clone Studio

**Create and test a real cloned voice in the browser. Replay the generated speech without rerunning AI, download WAV or MP3, and build a personalized single-voice EXE or install the full Windows suite.**

- **Browser voice studio:** https://tbenitz.github.io/A-Portable-Voice-Clone-Exe/
- **Windows installer:** https://github.com/tbenitz/A-Portable-Voice-Clone-Exe/releases/latest/download/VoiceCloneStudio-Setup.exe
- **Releases and builds:** https://github.com/tbenitz/A-Portable-Voice-Clone-Exe/releases
- **GitHub Actions status:** https://github.com/tbenitz/A-Portable-Voice-Clone-Exe/actions

This extends the **working Pocket TTS engine** from [supertonic3gpt-clone/pocket](https://tbenitz.github.io/supertonic3gpt-clone/pocket/). No fake voice outputs, hidden cloud-cloning APIs or separate Python install for normal Windows users.

## Browser Studio: voice audition and export

The published website is a full responsive voice studio based on the real ONNX/WASM Pocket TTS app.

1. Upload or record a voice you own or have permission to clone. Longer files are scanned to select a usable **10-second conditioning excerpt**; the model does not train on the whole long recording.
2. Choose your cloned voice, type a test, and press **Generate Audio** to use genuine Pocket TTS inference.
3. Listen to **A — Reference recording** and **B — Generated clone**. Replay B as many times as you like without regeneration. Completed PCM is retained independently of the streaming player.
4. Download the same output as WAV or browser-encoded MP3. The MP3 encoder is bundled and never sends the audio to a server.
5. Confirm permission and choose **Build my single-voice EXE**. The browser verifies the Windows EXE template's SHA-256 and PE header and appends a locally generated voice profile.

Nothing uploads voice references, generated audio, or the custom executable profile to GitHub. First-time Pocket TTS model downloads require internet access.

Browser inference is pinned to Hemanth HM's Pocket Voice revision a4496c429f1dcd6ea8faef55a21dc4b7d45636a9, as in the working repo.

## Two Windows editions

### Personal Edition — a single portable executable

A personalized Windows x64 EXE contains the native launcher, an embedded ZIP of the frozen Pocket inference GUI, and the specific voice's WAV. It opens its **own compact window**, not a web view: fixed cloned voice, text box, Real-time/Balanced/High/Ultra, speed/pitch/stability sliders, generation with streaming when possible, **Replay without regeneration**, WAV/MP3 exports, Stop, always-on-top and keyboard shortcuts.

The EXE reuses an extracted runtime under the user's local app data and downloads Pocket ONNX model weights once before subsequent offline operation. **No Python installation or external project checkout is necessary.** You can copy the generated EXE to another Windows x64 machine.

### Full Edition — installable studio or portable folder

A Windows Inno Setup installer creates Start Menu/Desktop shortcuts and an uninstaller, and includes all Python runtime libraries. A separately downloadable portable-folder build is also available under GitHub Actions.

It provides a multi-voice library, favorites/tags/notes, original and processed references, long-file health scan and cleaner, voice packs with optional cached ONNX weights, real Pocket synthesis, playback and replay, WAV/MP3, presets, stability/clarity/speed/pitch, microphone capture, Whisper transcription-to-speech, batch output, global hotkey, floating mini-panel, system tray, history and A/B, saved profiles and pronunciation replacements.

The full desktop application can **create personal single-voice EXEs from any saved voice** using the bundled Windows template, without recompiling software.

## Build and verification

GitHub Actions builds both the full frozen desktop program and the separate compact frozen GUI, plus the .NET NativeAOT Windows launcher; packages the personal voice template; checks plain and profile-embedded launchers; compiles and silently installs an Inno Setup installer; executes the installed EXE's self-test; uploads artifacts and creates a GitHub Release; and deploys the polished Pocket TTS web app via GitHub Pages.

Source development only: install requirements.txt and the pinned ONNX Python inference code with scripts/fetch_inference.py. Windows users do not run pip or install Python.

**Limits:** Physical Windows microphone, tray, global hotkey, output-device selection, and perceptual voice quality must be tested on a real Windows PC. A CI test of program startup does not prove speaker or microphone hardware behavior. The microphone speech-to-speech workflow is local Whisper ASR **then** Pocket TTS synthesis, not simultaneous zero-delay voice conversion. Ultra steps can be slower without being guaranteed better. Real emotion tags and optional cloud backup are not implemented. More reference minutes do not necessarily improve Pocket TTS, which conditions on a short sample.

## Open-source attribution and consent

- Website/desktop integration: **tbenitz**.
- Original browser program: [Hemanth HM Pocket Voice](https://github.com/hemanth/pocket-voice), MIT.
- [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts), under its model and code terms.
- [KevinAHM Pocket ONNX](https://huggingface.co/KevinAHM/pocket-tts-onnx) for native and browser conversion.
- [lamejs 1.2.1](https://www.jsdelivr.com/package/npm/lamejs), LGPL-3.0, licensed separately in the website's vendor directory.

Only clone voices you own or have permission to use; never present generated voice recordings as authentic testimony or use them for fraud.
