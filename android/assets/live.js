// Added by the Azan SIP TV app to the TV page (/tv) of Azan SIP or AthanBox, for Makkah live.
// Some Android TVs play Makkah live inside the page with a black picture, so the app plays it with
// Android's own video player instead (LivePlayer.java). The page still decides when it plays, from
// which link and whether it's quiet; this tells the app, and where the page's player is, and the
// app shows the picture there. "Use the page's player" in the app's menu leaves the page as it is.
(() => {
  const A = window.AzanTVApp, v = document.getElementById("video"), msg = document.getElementById("pmsg");
  if (!A || window.azanTV || !v || !msg || typeof LV !== "object") return;
  for (const f of ["startLive", "stopLive", "retryLive", "liveMsg", "soundBtn"])
    if (typeof window[f] !== "function") return;   // (a page without these: left as it is)
  window.azanTV = {};
  if (A.mode() !== "app") return;

  const RELAY_ = typeof RELAY === "string" ? RELAY : "/live/stream.m3u8";
  const src = () => typeof liveSrc === "function" ? liveSrc() : RELAY_;   // (AthanBox: maybe the stream's own link)
  // The page's own player never plays: no hls.js, and no built-in player for the stream. (Also for a
  // start the page had already begun before this was added: its "can't play" message is left out.)
  Object.defineProperty(window, "Hls", {configurable: false, get(){ return undefined; }, set(){}});
  v.canPlayType = () => "";
  const pageStop = stopLive, pageMsg = liveMsg;
  window.liveMsg = (kind, title, text) => {
    if (kind === "error" && /can't play Makkah live/.test(title || "")) return;
    pageMsg(kind, title, text);
  };

  let gen = 0, playing = false, relay = true, told = "";
  window.stopLive = () => { gen++; playing = false; pageStop(); A.stop(); told = ""; };
  window.startLive = async () => {
    stopLive();
    if (!LV.on || PREVIEW) return;
    const my = gen;
    liveMsg("wait", "Connecting to Makkah live…");
    soundBtn();
    if (typeof liveKey === "function") LV.key = liveKey();
    LV.played = false;
    const link = src();
    relay = link === RELAY_;
    if (relay){   // as the page does: Azan SIP says why when it hasn't got the stream
      try{
        const r = await fetch(RELAY_, {cache: "no-store"});
        if (!r.ok){ const j = await r.json().catch(() => ({})); throw new Error(j.error || `The stream isn't available (error ${r.status}).`); }
      }catch(e){ if (my === gen) retryLive("Makkah live isn't available right now", e.message); return; }
      if (my !== gen || !LV.on) return;
    }
    v.muted = !LV.want || LV.azanMuted;   // as the page's own player: its Sound button reads it
    soundBtn();
    A.play(new URL(link, location.href).href, relay);
  };
  window.azanTV = {   // the app's player
    playing(){
      if (!LV.on || playing) return;
      playing = LV.played = true;
      liveMsg(null); LV.wait = 15; soundBtn(); place();
    },
    failed(started, why){
      if (!LV.on) return;
      playing = false;
      if (!started && !relay && typeof directFailed === "function") return directFailed();   // through Azan SIP instead
      retryLive(started ? "Makkah live was interrupted" : "Makkah live isn't available right now", why || "");
    },
  };

  // Where the picture goes and whether it shows (not while the page shows a message over it), whether
  // it's quiet, and the "LIVE · Makkah" badge and Sound button to copy over it. In the page's pixels.
  const n = x => Math.round(x * 10) / 10;
  function clear(r){   // nothing of the page's in front of the player (a notice, "switched off", a sign-in)
    const box = v.parentElement;
    for (const [fx, fy] of [[.5, .5], [.2, .2], [.8, .2], [.2, .8], [.8, .8]]){
      const x = r.left + r.width * fx, y = r.top + r.height * fy;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const e = document.elementFromPoint(x, y);
      if (e && !box.contains(e)) return false;
    }
    return true;
  }
  function place(){
    const r = v.getBoundingClientRect(), b = v.parentElement.querySelector(".badge"), s = document.getElementById("sound");
    const show = LV.on && playing && msg.hidden && document.visibilityState === "visible" && r.width > 1 && r.height > 1 && clear(r);
    const o = {x: n(r.left), y: n(r.top), w: n(r.width), h: n(r.height), vw: innerWidth, show, muted: v.muted};
    if (b){
      const q = b.getBoundingClientRect();
      o.badge = {text: b.textContent.trim(), x: n(q.left - r.left), y: n(q.top - r.top), size: n(parseFloat(getComputedStyle(b).fontSize))};
    }
    if (s && !s.hidden && +getComputedStyle(s).opacity > .5){
      const q = s.getBoundingClientRect();
      o.sound = {text: s.textContent.trim(), x: n(r.right - q.right), y: n(r.bottom - q.bottom), size: n(parseFloat(getComputedStyle(s).fontSize))};
    }
    const now = JSON.stringify(o);
    if (now !== told){ told = now; A.layout(now); }
  }
  setInterval(place, 300);
  if (LV.on) startLive();   // already started with the page's own player: again with the app's
})();
