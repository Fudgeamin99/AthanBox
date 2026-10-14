// Added by the Azan SIP Client to the box's TV app on this screen: the "Screen settings" button,
// and "Connecting to the box" whenever the box can't be reached (back to the prayer times by itself).
(() => {
  const css = document.createElement("style");
  css.textContent = `
#azc-gear{position:fixed;left:50%;top:2vh;transform:translateX(-50%);z-index:9999;display:flex;gap:1vh;align-items:center;padding:1.2vh 2vh;border-radius:1.4vh;
  border:1px solid rgba(255,255,255,.25);background:rgba(0,0,0,.55);color:#fff;font:600 2.2vh system-ui,sans-serif;cursor:pointer;
  opacity:0;pointer-events:none;transition:opacity .3s}
#azc-gear.show{opacity:1;pointer-events:auto}
#azc-off{position:fixed;inset:0;z-index:9998;display:none;flex-direction:column;align-items:center;justify-content:center;gap:2vh;
  background:rgba(8,40,34,.92);color:#fff;font:500 3vh system-ui,sans-serif;text-align:center;padding:6vh}
#azc-off b{font-size:5vh}
#azc-off small{font-size:2.4vh;color:#cfe3dc;max-width:70vw}`;
  document.head.appendChild(css);
  const gear = document.createElement("button");
  gear.id = "azc-gear";
  gear.innerHTML = "&#9881; Screen settings";
  gear.onclick = () => location.assign("/client/");
  const off = document.createElement("div");
  off.id = "azc-off";
  off.innerHTML = "<b>Connecting to the Azan SIP Box…</b><small></small>";
  document.body.append(gear, off);

  let hide;
  const show = () => { gear.classList.add("show"); clearTimeout(hide); hide = setTimeout(() => gear.classList.remove("show"), 4000); };
  for (const ev of ["mousemove", "touchstart", "keydown"]) addEventListener(ev, show, true);
  addEventListener("keydown", e => { if ((e.key === "s" || e.key === "S") && !e.ctrlKey && !e.altKey) location.assign("/client/"); }, true);

  let misses = 0, gone = false;
  async function check(){
    let st = null;
    try{ st = await (await fetch("/client/api/status", {cache:"no-store"})).json(); }catch(e){}
    if (st && st.state === "ok"){
      misses = 0;
      if (gone) return location.reload();   // back: a fresh page, in case the box was updated
    } else if (st && ++misses >= 2){
      gone = true;
      off.style.display = "flex";
      off.querySelector("small").textContent = st.state === "not_allowed"
        ? "The box doesn't let TV screens connect. In its control panel, open TV app and switch on “Let TV screens connect”."
        : `The box at ${(st.box || "").replace(/^http:\/\//, "")} isn't answering. The prayer times come back by themselves when it does.`;
    }
    setTimeout(check, gone ? 2000 : 4000);
  }
  setTimeout(check, 4000);
})();
