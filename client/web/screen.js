// Added by the Azan SIP Client to the box's TV app on this screen: the volume buttons and the
// "Screen settings" button (shown when the mouse moves or a key is pressed), and "Connecting to the
// Azan SIP Box" whenever the box can't be reached (back to the prayer times by itself).
// On a second screen (a silent copy inside /client/wall), the sound is left to the first one.
(() => {
  const FRAMED = window.top !== window;
  const MIRROR = FRAMED && new URLSearchParams(location.search).get("mirror") === "1";
  if (MIRROR){   // Makkah live plays its sound once too, from the first screen
    const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "muted");
    Object.defineProperty(HTMLMediaElement.prototype, "muted", {configurable: true,
      get(){ return d.get.call(this); }, set(){ d.set.call(this, true); }});
    for (const m of document.querySelectorAll("video, audio")) d.set.call(m, true);
  }
  // Settings open on the whole screen, not inside one screen's copy
  const settings = () => top.location.assign("/client/");

  const css = document.createElement("style");
  css.textContent = `
#azc-bar{position:fixed;left:50%;top:2vh;transform:translateX(-50%);z-index:9999;display:flex;gap:1vh;align-items:center;padding:.8vh;border-radius:1.6vh;
  border:1px solid rgba(255,255,255,.25);background:rgba(0,0,0,.6);color:#fff;font:600 2.2vh system-ui,"DejaVu Sans",sans-serif;
  opacity:0;pointer-events:none;transition:opacity .3s;white-space:nowrap}
#azc-bar.show{opacity:1;pointer-events:auto}
#azc-bar button{display:flex;align-items:center;gap:.8vh;padding:1vh 1.8vh;border-radius:1.1vh;border:0;background:rgba(255,255,255,.12);color:#fff;font:inherit;cursor:pointer}
#azc-bar button:hover{background:rgba(255,255,255,.24)}
#azc-bar svg{width:2.8vh;height:2.8vh}
#azc-vol{min-width:17vh;text-align:center;font-variant-numeric:tabular-nums}
#azc-vol b{display:block;font-size:1.5vh;font-weight:500;color:#cfe3dc}
#azc-off{position:fixed;inset:0;z-index:9998;display:none;flex-direction:column;align-items:center;justify-content:center;gap:2vh;
  background:rgba(8,40,34,.92);color:#fff;font:500 3vh system-ui,sans-serif;text-align:center;padding:6vh}
#azc-off b{font-size:5vh}
#azc-off small{font-size:2.4vh;color:#cfe3dc;max-width:70vw}`;
  document.head.appendChild(css);
  const SPK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4z"/>';
  const bar = document.createElement("div");
  bar.id = "azc-bar";
  bar.innerHTML = `<button id="azc-down" title="Quieter (the - key)">${SPK}<path d="M15.5 12h5"/></svg></button>` +
    `<div id="azc-vol"><b>Azan volume</b><span></span></div>` +
    `<button id="azc-up" title="Louder (the + key)">${SPK}<path d="M15.5 12h5M18 9.5v5"/></svg></button>` +
    `<button id="azc-gear" title="The S key">&#9881; Screen settings</button>`;
  const off = document.createElement("div");
  off.id = "azc-off";
  off.innerHTML = "<b>Connecting to the Azan SIP Box…</b><small></small>";
  document.body.append(bar, off);

  let hide;
  const show = () => { bar.classList.add("show"); clearTimeout(hide); hide = setTimeout(() => bar.classList.remove("show"), 4000); };
  for (const ev of ["mousemove", "touchstart", "keydown"]) addEventListener(ev, show, true);

  // The volume of the azan on this screen (the TV app gets it from the client within half a second)
  let vol = null, changed = 0, ac = null;
  const showVol = () => { bar.querySelector("#azc-vol span").textContent = vol === null ? "" : vol ? vol + "%" : "Off"; };
  function beep(){   // a short note at the new volume, to hear how loud the azan will be
    if (!vol) return;
    try{
      ac = ac || new (window.AudioContext || window.webkitAudioContext)();
      const o = ac.createOscillator(), g = ac.createGain(), t = ac.currentTime + .03;
      o.frequency.value = 659.25; o.connect(g); g.connect(ac.destination);
      g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(.4 * vol / 100, t + .005); g.gain.exponentialRampToValueAtTime(.0001, t + .35);
      o.start(t); o.stop(t + .37);
    }catch(e){}
  }
  async function setVol(body){
    show(); changed = Date.now();
    try{
      const r = await (await fetch("/client/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})).json();
      if (r.ok){ vol = r.volume; showVol(); beep(); }
    }catch(e){}
  }
  let before = 50;
  const step = n => setVol({volume_step: n});
  const mute = () => { if (vol) { before = vol; setVol({volume: 0}); } else setVol({volume: before}); };
  bar.querySelector("#azc-down").onclick = () => step(-1);
  bar.querySelector("#azc-up").onclick = () => step(1);
  bar.querySelector("#azc-gear").onclick = settings;
  addEventListener("keydown", e => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key;
    if (k === "s" || k === "S") settings();
    else if (k === "+" || k === "=" || k === "AudioVolumeUp") step(1);
    else if (k === "-" || k === "_" || k === "AudioVolumeDown") step(-1);
    else if (k === "AudioVolumeMute") mute();
    else return;
    e.preventDefault();
  }, true);

  let misses = 0, gone = false;
  async function check(){
    let st = null;
    try{ st = await (await fetch("/client/api/status", {cache:"no-store"})).json(); }catch(e){}
    if (st && Date.now() - changed > 3000){ vol = st.volume; showVol(); }
    if (st && st.state === "ok"){
      misses = 0;
      if (gone) return location.reload();   // back: a fresh page, in case the box was updated
      // A second screen was plugged in: the prayer times on both
      if (!FRAMED && (st.outputs || []).length > 1 && (st.screens || []).length && location.pathname === "/tv") return location.replace("/");
    } else if (st && ++misses >= 2){
      gone = true;
      off.style.display = "flex";
      off.querySelector("small").textContent = st.state === "not_allowed"
        ? "The box doesn't let TV screens connect. In its control panel, open TV app and switch on “Let TV screens connect”."
        : `The box at ${(st.box || "").replace(/^http:\/\//, "")} isn't answering. The prayer times come back by themselves when it does.`;
    }
    setTimeout(check, gone ? 2000 : 4000);
  }
  check();
})();
