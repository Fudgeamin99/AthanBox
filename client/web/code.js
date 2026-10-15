// On the TV: the code to type on a phone or computer to open this screen's settings (shown while one
// asks for it), and "Play a test chime on the TV" pressed on one. Loaded by the prayer times
// (screen.js), the "Connecting" page and Screen settings. Does nothing on a phone or computer.
(() => {
  if (window.AZC_CODE) return;
  window.AZC_CODE = true;
  const MIRROR = window.top !== window && new URLSearchParams(location.search).get("mirror") === "1";
  const css = document.createElement("style");
  css.textContent = `
#azc-code{position:fixed;left:50%;bottom:6vh;transform:translateX(-50%);z-index:10000;display:none;flex-direction:column;align-items:center;gap:.6vh;
  padding:2.2vh 4vh;border-radius:2vh;border:1px solid rgba(255,255,255,.3);background:rgba(0,0,0,.82);color:#fff;
  font:500 2.4vh system-ui,"DejaVu Sans",sans-serif;text-align:center;box-shadow:0 1vh 4vh rgba(0,0,0,.4)}
#azc-code b{font-size:8vh;letter-spacing:.6vh;font-variant-numeric:tabular-nums;color:#f3e2b4;line-height:1.1}`;
  document.head.appendChild(css);
  const box = document.createElement("div");
  box.id = "azc-code";
  box.setAttribute("role", "status");
  box.innerHTML = "<span>Code for this screen's settings on a phone or computer</span><b></b>";
  document.body.appendChild(box);

  function chime(volume){   // the box's test chime, at this screen's volume
    const v = Math.max(0, Math.min(100, volume)) / 100;
    if (!v) return;
    try{
      const c = new (window.AudioContext || window.webkitAudioContext)();
      [659.25, 523.25, 587.33, 392.0].forEach((f, i) => {
        const o = c.createOscillator(), g = c.createGain(), t = c.currentTime + .1 + i * .6;
        o.frequency.value = f; o.connect(g); g.connect(c.destination);
        g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(.4 * v, t + .004); g.gain.exponentialRampToValueAtTime(.0001, t + .6);
        o.start(t); o.stop(t + .62);
      });
      setTimeout(() => c.close(), 3500);
    }catch(e){}
  }

  let last = null;
  async function poll(){
    let st = null;
    try{ st = await (await fetch("/client/api/status", {cache: "no-store"})).json(); }catch(e){}
    if (st && st.remote) return;
    if (st){
      const code = st.pair_code;
      box.style.display = code ? "flex" : "none";
      if (code) box.querySelector("b").textContent = code.slice(0, 3) + " " + code.slice(3);
      if (last === null) last = st.chime || 0;   // (not one pressed before this page opened)
      else if ((st.chime || 0) > last){ last = st.chime; if (!MIRROR) chime(st.volume); }
    }
    setTimeout(poll, 2000);
  }
  poll();
})();
