"""Turn the pinned upstream Pocket Voice page into the full Voice Clone Studio.

CI copies the exact known MIT upstream release before this script runs.
Fail on upstream drift rather than silently deploy broken controls.
"""
from pathlib import Path

root = Path("web")
page_file = root / "index.html"
app_file = root / "app.js"
page = page_file.read_text(encoding="utf-8")
app = app_file.read_text(encoding="utf-8")


def replace(old, new, count=1):
    global page
    if old not in page:
        raise SystemExit("Pinned upstream page changed. Missing " + repr(old[:75]))
    page = page.replace(old, new, count)


replace("<title>Pocket Voice — Clone any voice in your browser</title>",
        "<title>Pocket Voice Studio — Voice cloning, personal EXE and Windows desktop suite</title>")
replace('<meta name="author" content="Hemanth HM">','<meta name="author" content="tbenitz">')
replace('href="/ai/pocket-voice/favicon.svg"', 'href="./favicon.svg"')
replace('href="/ai/pocket-voice/og-image.png"', 'href="./favicon.svg"')
replace('<meta name="theme-color" content="#050504">', '<meta name="theme-color" content="#070d17">')
replace('<link rel="stylesheet" href="style.css?v=2.0.9">',
        '<link rel="stylesheet" href="style.css?v=2.0.9">\n'
        '<link rel="stylesheet" href="./theme.css">')
replace("Clone any voice.<br><em>In your browser.</em>",
        "Create a voice.<br><em>Take it anywhere.</em>")
replace("Record your voice, clone it, type some text, and hear it speak.<br>Runs entirely in your browser via ONNX and WebAssembly. No server. No API keys. Your data stays on your machine.",
        "Upload or record a voice. Hear a real Pocket TTS sample, replay it, save WAV or MP3, then build a personal Windows EXE or install the complete voice studio. No API keys. Your voice stays on your device.")
replace("ONNX Runtime · 100% Client-Side", "POCKET TTS · PRIVATE VOICE CLONING · WINDOWS DESKTOP")
replace("<h2 class=\"app-section__title\">Voice Studio</h2>",
        "<h2 class=\"app-section__title\">Make your voice.</h2>")
replace("Clone a voice, select a preset, type text, generate.",
        "Step 01 · Upload or record a reference, type an example and hear your cloned speech. Step 02 · Replay and download. Step 03 · Get your Windows app.")
replace("Try it now", "Open voice studio")
replace("Record ~5s of speech or upload an audio clip to clone",
        "Speak clearly for 5–10 seconds, or upload a recording. Pocket TTS selects a short conditioning excerpt.")
replace("Built with ❤️ by <a href=\"https://h3manth.com\" target=\"_blank\" rel=\"noopener\">Hemanth HM</a> · Powered by <a href=\"https://github.com/kyutai-labs/pocket-tts\" target=\"_blank\" rel=\"noopener\">Pocket TTS</a>",
        "Studio adaptation by <a href=\"https://github.com/tbenitz\" target=\"_blank\" rel=\"noopener\">tbenitz</a> · Powered by Kyutai Pocket TTS · <a href=\"./ATTRIBUTION.txt\">Open source credits</a>")
replace('<meta name="twitter:creator" content="@anthropic">', "")
replace('content="https://h3manth.com/ai/pocket-voice/og-image.png"', 'content="./favicon.svg"')
replace('"name": "Pocket Voice"', '"name": "Pocket Voice Studio"')
replace('"creator": { "@type": "Person", "name": "Hemanth HM", "url": "https://h3manth.com" }',
        '"creator": { "@type": "Person", "name": "tbenitz", "url": "https://github.com/tbenitz" }')
replace('<body>', '<body>\n<header class="studio-nav"><a class="studio-logo" href="#hero"><span class="logo-mark">◉</span> POCKET<span>STUDIO</span></a><nav aria-label="Site"><a href="#app">Voice creator</a><a href="#studio-experience">Replay &amp; exports</a><a href="#download-suite">Windows software</a><a href="https://github.com/tbenitz/A-Portable-Voice-Clone-Exe" target="_blank" rel="noopener">GitHub</a></nav></header>')
replace('<script type="module" src="app.js?v=2.0.9"></script>',
        '<script type="module" src="app.js?v=2.0.9"></script>\n'
        '<script src="./vendor/lame.min.js"></script>\n'
        '<script type="module" src="./studio-addon.js"></script>')
if "new PocketVoice();" not in app:
    raise SystemExit("Pinned Pocket Voice main constructor changed.")
app = app.replace("new PocketVoice();", "window.__pocketApp = new PocketVoice();", 1)
# Use only already downloaded PCM. Originals are untouched, so the working
# VoiceEncoder / generation logic remains the same and can be regression-tested.
page_file.write_text(page, encoding="utf-8")
app_file.write_text(app, encoding="utf-8")
print("Pocket Studio: working Pocket browser model, local replay, and EXE exporter wired.")
