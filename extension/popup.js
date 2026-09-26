"use strict";

const $ = (id) => document.getElementById(id);
const msg = (text, kind) => { const m = $("msg"); m.textContent = text; m.className = "msg " + (kind || "info"); };

// Pairing code = base64url(JSON{u: <site origin>, c: <signed connect code>}).
function decodePairing(raw) {
  try {
    const s = raw.trim().replace(/\s+/g, "").replace(/-/g, "+").replace(/_/g, "/");
    const data = JSON.parse(atob(s + "===".slice((s.length + 3) % 4)));
    if (!data.u || !data.c) return null;
    const url = new URL(data.u);
    if (!/^https?:$/.test(url.protocol)) return null;
    return { origin: url.origin, code: data.c };
  } catch (e) { return null; }
}

function show(view) {
  $("pair-view").style.display = view === "pair" ? "" : "none";
  $("connect-view").style.display = view === "connect" ? "" : "none";
}

async function getPairing() {
  const { pairing } = await chrome.storage.local.get("pairing");
  return pairing || null;
}

async function init() {
  const p = await getPairing();
  if (p) {
    show("connect");
    $("paired-to").textContent = new URL(p.origin).host;
  } else {
    show("pair");
  }
}

$("pair-btn").addEventListener("click", async () => {
  const parsed = decodePairing($("pair-input").value);
  if (!parsed) { msg("That pairing code doesn't look right. Copy it again from the Connect page.", "bad"); return; }
  // Ask for permission to talk to this site's origin.
  const granted = await chrome.permissions.request({ origins: [parsed.origin + "/*"] }).catch(() => false);
  if (!granted) { msg("Permission to reach your site was declined.", "bad"); return; }
  await chrome.storage.local.set({ pairing: parsed });
  msg("Paired with " + new URL(parsed.origin).host + ". Now log in to udemy.com and connect.", "ok");
  await init();
});

$("unpair").addEventListener("click", async () => {
  await chrome.storage.local.remove("pairing");
  msg("", "info"); $("msg").className = "msg";
  await init();
});

async function readCookie(name) {
  const c = await chrome.cookies.get({ url: "https://www.udemy.com", name });
  return c ? c.value : "";
}

$("connect-btn").addEventListener("click", async () => {
  const p = await getPairing();
  if (!p) { await init(); return; }
  const btn = $("connect-btn");
  btn.disabled = true; btn.textContent = "Connecting...";
  msg("Reading your Udemy login...", "info");
  try {
    const token = await readCookie("access_token");
    if (!token) {
      msg("You're not logged in to udemy.com in this browser. Log in there, then try again.", "bad");
      btn.disabled = false; btn.textContent = "Connect my Udemy account"; return;
    }
    const clientId = await readCookie("client_id");
    const body = new URLSearchParams({ t: token, c: clientId, code: p.code });
    const res = await fetch(p.origin + "/connect/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      body,
    });
    let data = null; try { data = await res.json(); } catch (e) {}
    if (res.ok && data && data.ok) {
      msg("Connected " + (data.name || "your Udemy account") + "! You can open your dashboard.", "ok");
      $("foot").innerHTML = '<a class="link" id="open-dash">Open dashboard &rarr;</a>';
      const od = $("open-dash"); if (od) od.addEventListener("click", () => chrome.tabs.create({ url: p.origin + "/dashboard" }));
    } else {
      msg((data && data.error) || "Could not connect (HTTP " + res.status + "). Re-copy the pairing code if it was reset.", "bad");
    }
  } catch (e) {
    msg("Could not reach your site. Check it's online and re-pair if the address changed.", "bad");
  }
  btn.disabled = false; btn.textContent = "Connect my Udemy account";
});

init();
