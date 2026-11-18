// ARGUS's additions to PyLabRobot's 3D viewer page (served by ARGUS at /viewer/, see argus/viewer.py).
//
//  - Points the page at the live viewer, or at a run's recording that ARGUS replays.
//  - Docks the head panels (96-head, pipetting channels, gripper arm) in a column at the right edge, smaller,
//    instead of over the deck: still visible, out of the way of the machine.
//  - Collapses the inspector, so the deck gets the width.
//  - With record: drives the viewer's own GIF recorder over the whole replay and sends the GIF to ARGUS.
(() => {
  const cfg = window.ARGUS_VIEW || {};
  const wsUrl = (u) => (u.startsWith("/") ? `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${u}` : u);
  if (cfg.ws) window.WS_URL = wsUrl(cfg.ws);

  // Replay messages of ARGUS's own ("argus" events) are ignored by the viewer; this page listens for them.
  let socket = null;
  const NativeWS = window.WebSocket;
  window.WebSocket = class extends NativeWS {
    constructor(...args) {
      super(...args);
      socket = this;
      this.addEventListener("message", (e) => {
        if (typeof e.data !== "string") return;
        if (e.data.startsWith('{"event": "argus"')) {
          try { const m = JSON.parse(e.data); window.dispatchEvent(new CustomEvent("argus:" + m.data.kind, { detail: m.data })); } catch (_) {}
        } else if (e.data.startsWith('{"event": "state"')) {
          window.dispatchEvent(new Event("argus:state"));
        }
      });
    }
  };
  const tellParent = (msg) => { try { window.parent.postMessage({ argus: true, ...msg }, location.origin); } catch (_) {} };

  // ---------------------------------------------------------------- the head panels, docked
  const SCALE = 0.55, GAP = 8, EDGE = 10;
  const ORDER = ["multi", "single", "arm"];
  const style = document.createElement("style");
  style.textContent = `
    .dt-panel { left: auto !important; right: ${EDGE}px !important; top: var(--argus-top, ${EDGE}px) !important;
                transform: scale(${SCALE}); transform-origin: top right; opacity: .94; pointer-events: auto; }
    .dt-panel .dt-reset { display: none !important; }
    #argus-badge { position: absolute; left: 50%; transform: translateX(-50%); bottom: 10px; z-index: 30;
                   background: rgba(17, 17, 17, .78); color: #fff; font: 600 12px system-ui, sans-serif;
                   padding: 5px 12px; border-radius: 999px; pointer-events: none; }`;
  document.head.appendChild(style);
  function dock() {
    const panels = [...document.querySelectorAll(".dt-panel")];
    panels.sort((a, b) => ORDER.findIndex((k) => a.classList.contains(`mt-panel-${k}`)) - ORDER.findIndex((k) => b.classList.contains(`mt-panel-${k}`)));
    let top = EDGE;
    for (const p of panels) {
      p.style.setProperty("--argus-top", `${top}px`);
      top += p.offsetHeight * SCALE + GAP;
    }
  }
  new MutationObserver(dock).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(dock, 500);

  // ---------------------------------------------------------------- the inspector, collapsed once
  const collapseInspector = setInterval(() => {
    const side = document.getElementById("sidepanel"), toggle = document.getElementById("toolbar-right-toggle");
    if (!window.__plrBooted || !side || !toggle) return;
    clearInterval(collapseInspector);
    if (!side.classList.contains("collapsed")) toggle.click();
    // Frame the deck again for the wider viewport (the reset-view button's start view).
    setTimeout(() => document.getElementById("home-button")?.click(), 300);
  }, 200);

  // ---------------------------------------------------------------- replay: start once the deck is drawn
  let badge = null;
  function say(text) {
    const main = document.querySelector("main");
    if (!main) return;
    if (!badge) { badge = document.createElement("div"); badge.id = "argus-badge"; main.appendChild(badge); }
    badge.textContent = text;
    badge.hidden = !text;
  }
  if (cfg.mode === "replay") {
    let asked = false;
    window.addEventListener("argus:state", () => {
      if (asked) return;
      asked = true;
      // The scene's meshes load after its first message: give them a moment, then let the replay run.
      setTimeout(() => {
        if (cfg.record) startRecording();
        try { socket.send(JSON.stringify({ event: "argus_go" })); } catch (_) {}
        say(cfg.record ? "● Recording GIF" : "▶ Replay");
        tellParent({ kind: "replay-start" });
      }, 1800);
    });
    window.addEventListener("argus:end", () => {
      say(cfg.record ? "Rendering GIF…" : "Replay finished");
      tellParent({ kind: "replay-end" });
      if (cfg.record) setTimeout(stopRecording, 800);
    });
  }

  // ---------------------------------------------------------------- the viewer's GIF recorder, driven
  const MAX_FRAMES = 140; // the viewer stops at 150
  function startRecording() {
    const slider = document.getElementById("gif-frame-rate");
    // A frame every interval*20 ms; the interval is set so the whole run fits in the frames a GIF may hold.
    const interval = Math.max(4, Math.ceil(((cfg.duration_s || 30) * 1000) / (MAX_FRAMES * 20)));
    if (slider) { slider.value = String(interval); slider.dispatchEvent(new Event("input")); }
    document.getElementById("start-recording-button")?.click();
  }
  function stopRecording() {
    document.getElementById("stop-recording-button")?.click();
    // The viewer renders the GIF, then shows its download button; the blob is taken from that download.
    const realCreate = URL.createObjectURL.bind(URL);
    const realClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = (blob) => {
      if (blob instanceof Blob && blob.type === "image/gif") upload(blob);
      return realCreate(blob);
    };
    HTMLAnchorElement.prototype.click = function () {
      if (this.download && this.download.endsWith(".gif")) return; // ARGUS keeps the GIF; no download here
      return realClick.call(this);
    };
    const ready = setInterval(() => {
      const box = document.getElementById("gif-download");
      if (box && box.style.display === "flex") {
        clearInterval(ready);
        document.getElementById("gif-download-button").click();
      }
      const progress = document.getElementById("progressBar");
      if (progress && progress.textContent) say(progress.textContent);
    }, 250);
  }
  async function upload(blob) {
    say("Saving GIF…");
    try {
      const r = await fetch(`/api/runs/${cfg.run}/gif`, { method: "POST", body: blob, headers: { "Content-Type": "image/gif" } });
      if (!r.ok) throw new Error(await r.text());
      say("GIF saved");
      tellParent({ kind: "gif-saved", bytes: blob.size });
    } catch (e) {
      say("GIF could not be saved");
      tellParent({ kind: "gif-failed", message: String(e) });
    }
  }
})();
