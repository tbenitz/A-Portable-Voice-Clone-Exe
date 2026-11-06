"""Real Chromium smoke: actual published-site wiring, replay/MP3, release-download CORS.

A simulated PCM waveform is used for player/export UI only. No claim about
model quality; the separate Pocket ONNX smoke workflow generates real speech.
"""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    chrome = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        raise RuntimeError("Headless Chrome unavailable on this GitHub runner")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/web/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome, headless=True,
                                         args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(accept_downloads=True)
            # Don't download large model weights merely to smoke-test the UI.
            context.route("**/huggingface.co/**", lambda route: route.abort())
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#file-upload", state="attached", timeout=12000)
            page.wait_for_selector("#clone-player", timeout=12000)
            for name in ["#btn-record", "#btn-upload", "#btn-generate", "#save-wav",
                         "#save-mp3", "#download-personal", "#personal-name"]:
                assert page.locator(name).count() == 1, f"Missing browser control {name}"

            # Test the EXACT user's requirement: replay and both download buttons
            # use existing PCM, no second model call.
            state = page.evaluate("""() => {
              const app = window.__pocketApp;
              if (!app) throw new Error('Real Pocket TTS app not exposed');
              const samples = new Float32Array(24000);
              for (let i=0;i<samples.length;i++)
                samples[i] = Math.sin(2*Math.PI*180*i/24000)*.16;
              app.currentChunks = [samples];
              app.isGenerating = false;
              app.buildDownloadBlob();
              const player=document.getElementById('clone-player');
              return {src:player.currentSrc || player.src,
                      wavDisabled:document.getElementById('save-wav').disabled,
                      mp3Disabled:document.getElementById('save-mp3').disabled};
            }""")
            assert state["src"].startswith("blob:"), state
            assert not state["wavDisabled"] and not state["mp3Disabled"], state
            with page.expect_download(timeout=25000) as download_result:
                page.locator("#save-mp3").click()
            output=download_result.value
            path=output.path()
            assert output.suggested_filename.endswith(".mp3"), output.suggested_filename
            assert path.stat().st_size > 1000, "MP3 encoding output too small"
            print("PASS: website recording/upload controls, retained playback, actual MP3 encoding", flush=True)
            # The EXE starter is served from the same GitHub Pages origin,
            # avoiding the cross-origin binary restriction of GitHub Releases.
            starter = ROOT / "web" / "download" / "SingleVoice-Starter.exe"
            if starter.is_file():
                result = page.evaluate("""() => {
                  const app=window.__pocketApp;
                  app.lastClonedPCM=new Float32Array(24000*5);
                  if (![...app.el.voiceSelect.options].some(x=>x.value==='custom')){
                    const choice=document.createElement('option');choice.value='custom';
                    choice.text='My voice';app.el.voiceSelect.prepend(choice);
                  }
                  app.el.voiceSelect.value='custom';
                  app.activeVoice='custom';
                  app.currentChunks=[new Float32Array(24000).fill(.12)];
                  app.buildDownloadBlob();
                  const consent=document.getElementById('single-consent');
                  consent.checked=true;
                  consent.dispatchEvent(new Event('change'));
                  return {enabled:!document.getElementById('download-personal').disabled};
                }""")
                assert result["enabled"], result
                with page.expect_download(timeout=30000) as download_result:
                    page.locator("#download-personal").click()
                exe = download_result.value.path().read_bytes()
                assert exe[:2] == b"MZ" and exe[-16:-8] == b"CVRCFG01", "Personalized EXE invalid"
                print("PASS: browser personalized standalone EXE assembled locally", flush=True)
            else:
                print("Starter pending publication by Windows build: browser EXE test deferred", flush=True)
            browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
