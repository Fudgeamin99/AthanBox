// AthanBox: shared helpers for the setup wizard (setup.html) and the prayer-times screen (display.html).
const $ = (s, el=document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const TITLE = {fajr:"Fajr", sunrise:"Sunrise", dhuhr:"Dhuhr", asr:"Asr", maghrib:"Maghrib", isha:"Isha", jumuah:"Jumu'ah"};
const ARABIC = {fajr:"الفجر", sunrise:"الشروق", dhuhr:"الظهر", asr:"العصر", maghrib:"المغرب", isha:"العشاء", jumuah:"الجمعة"};
const LOGO = `<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="16" fill="#146356"/><path fill="#f4e3b5" d="M18.6 8.3a8 8 0 1 0 5.2 13.6A6.6 6.6 0 0 1 18.6 8.3z"/><path fill="#f4e3b5" d="M21.3 11.6l.62 1.28 1.41.2-1.02.99.24 1.4-1.25-.66-1.25.66.24-1.4-1.02-.99 1.41-.2z"/></svg>`;

class ApiError extends Error { constructor(msg, status, body){ super(msg); this.status = status; this.body = body; } }

async function api(path, body, raw){
  const opt = {method: body === undefined ? "GET" : "POST", headers: {"X-Azan-Panel": "1"}};
  if(body !== undefined){
    if(raw){ opt.body = body; opt.headers["Content-Type"] = "application/octet-stream"; }
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  let r;
  try { r = await fetch(path, opt); } catch(e){ throw new ApiError("Can't reach AthanBox. Check it's switched on and connected.", 0, {}); }
  const j = await r.json().catch(() => ({ok:false, error:"Unexpected reply"}));
  if(!r.ok || j.ok === false) throw new ApiError(j.error || ("Error " + r.status), r.status, j);
  return j;
}

// "in 37 min", "in 2 h 5 min"
function untilText(seconds){
  const m = Math.max(0, Math.ceil(seconds / 60));
  if(m < 1) return "now";
  if(m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return `in ${h} h` + (r ? ` ${r} min` : "");
}

// Recordings uploaded from a phone or PC are made phone-quality (8 kHz mono WAV) in the browser.
async function toPhoneWav(file){
  const data = await file.arrayBuffer();
  const AC = window.AudioContext || window.webkitAudioContext; const ctx = new AC();
  const decoded = await ctx.decodeAudioData(data.slice(0)); ctx.close();
  const rate = 8000, len = Math.ceil(decoded.duration * rate);
  const off = new OfflineAudioContext(1, len, rate);
  const src = off.createBufferSource(); src.buffer = decoded;
  const hp = off.createBiquadFilter(); hp.type = "highpass"; hp.frequency.value = 180;
  const comp = off.createDynamicsCompressor(); comp.threshold.value = -20; comp.ratio.value = 3; comp.attack.value = 0.01; comp.release.value = 0.25;
  src.connect(hp); hp.connect(comp); comp.connect(off.destination); src.start();
  const out = (await off.startRendering()).getChannelData(0);
  let peak = 0; for(let i = 0; i < out.length; i++){ const v = Math.abs(out[i]); if(v > peak) peak = v; }
  const gain = peak > 0 ? 0.89 / peak : 1;
  const buf = new ArrayBuffer(44 + out.length * 2), v = new DataView(buf);
  const w = (o, s) => { for(let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + out.length * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, out.length * 2, true);
  for(let i = 0; i < out.length; i++){ const s = Math.max(-1, Math.min(1, out[i] * gain)); v.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true); }
  return buf;
}
