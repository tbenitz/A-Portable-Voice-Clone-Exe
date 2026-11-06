/**
 * Extends the pinned, already-working Pocket Voice app.
 * No mocked voice outputs; saved PCM powers replay and WAV/MP3 exports.
 */
(async () => {
  "use strict";
  const app = window.__pocketApp;
  if (!app) { console.error("Pocket TTS app not initialized"); return; }
  const PCM_RATE = 24000;
  const encode = new TextEncoder();
  const container = document.querySelector("#app .container");
  if (!container) throw new Error("Upstream Pocket Voice layout changed.");
  const result = await fetch("./studio-ui.html");
  if (!result.ok) throw new Error("Studio download and playback panel missing.");
  container.insertAdjacentHTML("beforeend", await result.text());
  const el = id => document.getElementById(id);
  const original = el("reference-player");
  const generated = el("clone-player");
  const btnWav = el("save-wav");
  const btnMp3 = el("save-mp3");
  const btnExe = el("download-personal");
  const consent = el("single-consent");
  const status = el("personal-status");
  let voiceSerial = 0;
  let previewSerial = -1;
  let previewVoice = null;
  let audioPCM = null;
  let wavUrl = null;
  let refUrl = null;
  let busy = false;

  function message(value, isError = false) {
    status.textContent = String(value);
    status.classList.toggle("is-error", isError);
  }
  function customSelected() {
    return app.activeVoice === "custom" && app.el.voiceSelect.value === "custom" &&
           !!app.lastClonedPCM;
  }
  function canExportExe() {
    return !busy && !app.isGenerating && !app.isVoiceEncoding && consent.checked &&
      customSelected() && app.lastClonedPCM.length >= PCM_RATE * 3 &&
      previewSerial === voiceSerial && previewVoice === "custom" &&
      !!audioPCM && audioPCM.length > PCM_RATE / 3;
  }
  function update() {
    btnExe.disabled = !canExportExe();
    btnWav.disabled = !audioPCM;
    btnMp3.disabled = !audioPCM || busy;
    btnExe.setAttribute("aria-busy", busy ? "true" : "false");
  }
  function mergeChunks(chunks) {
    const length = chunks.reduce((sum, c) => sum + c.length, 0);
    if (length <= 0 || length > PCM_RATE * 60 * 60 * 2) throw new Error("Invalid output length");
    const audio = new Float32Array(length);
    let pos = 0;
    for (const chunk of chunks) { audio.set(chunk, pos); pos += chunk.length; }
    return audio;
  }
  function audioBlob(pcm) {
    return new Blob([app.float32ToWav(pcm, PCM_RATE)], {type: "audio/wav"});
  }
  function audioLink(pcm) { return URL.createObjectURL(audioBlob(pcm)); }
  function showReference() {
    if (refUrl) URL.revokeObjectURL(refUrl);
    refUrl = null;
    original.removeAttribute("src");
    if (customSelected() && app.lastClonedPCM.length) {
      refUrl = audioLink(app.lastClonedPCM);
      original.src = refUrl;
      original.load();
      const health = app.referenceHealth;
      el("reference-note").textContent = health ?
        ("Voice health " + health.score + "/100 (signal estimate) · " + health.originalDuration.toFixed(1) +
         "s uploaded · excerpt starts at " + health.start + "s · " + health.hint) :
        ((app.lastClonedPCM.length / PCM_RATE).toFixed(1) + " seconds of the original reference. Replay any time.");
    } else {
      el("reference-note").textContent = "Choose the cloned voice to hear its original reference recording.";
    }
    update();
  }

  const worker = app.handleWorkerMessage.bind(app);
  app.handleWorkerMessage = (...args) => {
    const answer = worker(...args);
    if (args[0] && (args[0].type === "voice_encoded" || args[0].type === "voice_set")) showReference();
    return answer;
  };
  const process = app.processVoiceClip.bind(app);
  app.processVoiceClip = async (...args) => {
    voiceSerial++;
    previewSerial = -1;
    message("Encoding the new reference locally. Generate a voice example once it is ready.");
    update();
    return process(...args);
  };
  const load = app.loadSavedVoice.bind(app);
  app.loadSavedVoice = async (...args) => {
    voiceSerial++;
    previewSerial = -1;
    app.referenceHealth = null;
    update();
    return load(...args);
  };
  const buildAudio = app.buildDownloadBlob.bind(app);
  app.buildDownloadBlob = (...args) => {
    const output = buildAudio(...args);
    if (app.currentChunks.length && !app.isGenerating) {
      const merged = mergeChunks(app.currentChunks);
      if (wavUrl) URL.revokeObjectURL(wavUrl);
      wavUrl = audioLink(merged);
      generated.src = wavUrl;
      generated.load();
      audioPCM = merged;
      previewVoice = app.activeVoice;
      previewSerial = customSelected() ? voiceSerial : -1;
      el("generated-note").textContent = (merged.length / PCM_RATE).toFixed(1) +
        "s saved. Replay using the controls. Export without another voice generation.";
      message(customSelected()
        ? "Preview finished. Tick the permission box to create your single-voice executable."
        : "Audio saved. Generate using your custom uploaded voice to unlock the personal EXE.");
    }
    update();
    return output;
  };
  const generate = app.generate.bind(app);
  app.generate = async (...args) => {
    if (!app.isWorkerReady || app.isGenerating || !app.el.textInput.value.trim()) return;
    message("Generating a real Pocket TTS preview locally...");
    const output = await generate(...args);
    update();
    return output;
  };
  const stop = app.stop.bind(app);
  app.stop = (...args) => {
    const response = stop(...args);
    message("Stopped. The last complete preview remains available.");
    update();
    return response;
  };
  app.el.voiceSelect.addEventListener("change", () => requestAnimationFrame(showReference));
  consent.addEventListener("change", update);

  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  btnWav.addEventListener("click", () => {
    if (audioPCM) download(audioBlob(audioPCM), "Pocket-Voice-Generated.wav");
  });
  btnMp3.addEventListener("click", async () => {
    if (!audioPCM || busy) return;
    busy = true; update();
    try {
      if (!window.lamejs || !window.lamejs.Mp3Encoder)
        throw new Error("The local MP3 encoder was not loaded.");
      const mp3 = new window.lamejs.Mp3Encoder(1, PCM_RATE, 192);
      const blocks = [];
      for (let i = 0; i < audioPCM.length; i += 1152) {
        const pcm = audioPCM.subarray(i, i + 1152);
        const shorts = new Int16Array(pcm.length);
        for (let j = 0; j < pcm.length; j++) {
          const sample = Math.max(-1, Math.min(1, pcm[j]));
          shorts[j] = Math.round(sample * (sample < 0 ? 32768 : 32767));
        }
        const bytes = mp3.encodeBuffer(shorts);
        if (bytes.length) blocks.push(new Uint8Array(bytes));
        if (i % (1152 * 300) === 0) await new Promise(resolve => setTimeout(resolve, 0));
      }
      const last = mp3.flush();
      if (last.length) blocks.push(new Uint8Array(last));
      if (!blocks.length) throw new Error("Encoder returned no MP3 frames.");
      download(new Blob(blocks, {type:"audio/mpeg"}), "Pocket-Voice-Generated.mp3");
      message("MP3 saved from the existing generated audio, no regeneration needed.");
    } catch (error) {
      message("MP3 export error: " + error.message, true);
    } finally { busy = false; update(); }
  });

  function base64(arrayBuffer) {
    const bytes = new Uint8Array(arrayBuffer), chunks = [];
    for (let i = 0; i < bytes.length; i += 32768) {
      chunks.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
    }
    return btoa(chunks.join(""));
  }
  function ascii(bytes, start, size) {
    return new TextDecoder("ascii").decode(bytes.subarray(start, start + size));
  }
  // Pages can temporarily serve an older build after a release. The second
  // source is the repository's committed, already-tested 6 MB Windows starter.
  // Both paths receive the same SHA-256 verification before any executable
  // is built. The voice recording is only appended locally in this browser.
  const STARTER_PATHS = [
    {meta: "./download/starter-build.json", exe: "./download/SingleVoice-Starter.exe"},
    {meta: "https://raw.githubusercontent.com/tbenitz/A-Portable-Voice-Clone-Exe/main/web/download/starter-build.json",
     exe: "https://raw.githubusercontent.com/tbenitz/A-Portable-Voice-Clone-Exe/main/web/download/SingleVoice-Starter.exe"}
  ];
  // A verified copy from the published Windows CI build. Only used if the
  // metadata endpoints are unavailable; a mismatching binary is still rejected.
  const VERIFIED_STARTER = {
    size: 6102016, thin: true, revision: "259d71d38b",
    architecture: "windows-x64",
    sha256: "4341e0068e5beb30460bccaf1b69927921ed7ff6dd2905fa0d1788dd518073eb"
  };
  function validateStarterInfo(info) {
    return !!info && info.architecture === "windows-x64" &&
      info.thin === true && Number.isSafeInteger(info.size) &&
      info.size > 200000 && info.size < 40000000 &&
      /^[a-f0-9]{64}$/i.test(info.sha256);
  }
  async function loadTemplate() {
    message("Preparing the verified single-voice Windows starter…");
    const errors = [];
    for (const source of STARTER_PATHS) {
      try {
        let metadata;
        try {
          const metaResponse = await fetch(source.meta, {cache: "no-store"});
          if (!metaResponse.ok) throw new Error("HTTP " + metaResponse.status);
          metadata = await metaResponse.json();
        } catch (error) {
          errors.push("metadata " + source.meta + ": " + error.message);
          // Known build fallback is safe only if SHA-256 of the binary matches.
          metadata = VERIFIED_STARTER;
        }
        if (!validateStarterInfo(metadata)) throw new Error("Invalid verified Windows starter manifest");
        const result = await fetch(source.exe, {cache: "no-store"});
        if (!result.ok) throw new Error("HTTP " + result.status);
        const binary = await result.arrayBuffer();
        if (binary.byteLength !== metadata.size) {
          throw new Error("Starter size mismatch (" + binary.byteLength + " versus " + metadata.size + ")");
        }
        const bytes = new Uint8Array(binary);
        const view = new DataView(binary);
        if (bytes.length < 128) throw new Error("Windows launcher is incomplete");
        const peAt = view.getUint32(0x3c, true);
        if (ascii(bytes, 0, 2) !== "MZ" || peAt < 64 || peAt + 6 > bytes.length ||
            ascii(bytes, peAt, 4) !== "PE\u0000\u0000" ||
            view.getUint16(peAt + 4, true) !== 0x8664) {
          throw new Error("Windows x64 executable header is invalid");
        }
        const digest = await crypto.subtle.digest("SHA-256", binary);
        const actual = [...new Uint8Array(digest)]
          .map(value => value.toString(16).padStart(2, "0")).join("");
        if (actual !== metadata.sha256.toLowerCase()) {
          throw new Error("Starter checksum mismatch — refusing an unverified executable");
        }
        return {binary, metadata};
      } catch (error) {
        errors.push(source.exe + ": " + (error.message || error));
      }
    }
    throw new Error("The Windows starter could not be retrieved from the site or GitHub. " +
      "Please reload the page and retry. (" + errors.join("; ").slice(0, 500) + ")");
  }

  btnExe.addEventListener("click", async () => {
    if (!canExportExe()) { message("Generate a sample with your cloned voice, then confirm consent.", true); return; }
    busy = true; update();
    try {
      const allowed = new Set(["english_2026-04","english","german","italian","spanish","portuguese"]);
      const lang = app.el.langSelect.value;
      if (!allowed.has(lang)) throw new Error("Unsupported language.");
      const pcm = app.lastClonedPCM;
      if (pcm.length > PCM_RATE * 20) throw new Error("Reference too long for the compact model conditioning sample.");
      const {binary, metadata} = await loadTemplate();
      message("EXE checksum verified. Embedding your cloned voice entirely in this browser...");
      const referenceWav = app.float32ToWav(pcm, PCM_RATE);
      const payload = encode.encode(JSON.stringify({
        version: 1, engine: "pocket", lang, voice_name: (el("personal-name").value || "My Cloned Voice").trim().slice(0,72),
        voice_wav_b64: base64(referenceWav)
      }));
      if (payload.length > 8000000) throw new Error("Embedded voice is larger than 8 MB.");
      const footer = new Uint8Array(16);
      footer.set(encode.encode("CVRCFG01"));
      new DataView(footer.buffer).setBigUint64(8, BigInt(payload.length), true);
      download(new Blob([binary, payload, footer], {type:"application/octet-stream"}),
               "PocketVoice-Personal-" + metadata.revision + ".exe");
      message("Your personal voice EXE is ready. First run on Windows downloads and verifies its ~205 MB runtime and Pocket model once; later launches reuse the local cache.");
    } catch (e) {
      message("EXE creation failed: " + (e.message || e), true);
    } finally { busy = false; update(); }
  });
  window.addEventListener("beforeunload", () => {
    if (wavUrl) URL.revokeObjectURL(wavUrl);
    if (refUrl) URL.revokeObjectURL(refUrl);
  });
  update();
})();
