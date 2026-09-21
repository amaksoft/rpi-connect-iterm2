// Background: attach real cookies (HttpOnly) then forward to native host.
const HOST = "com.rpi.shell";
const ALLOWED = /^https:\/\/connect\.raspberrypi\.com\//;
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      const tabUrl = sender.tab?.url || "";
      const msgUrl = sender.url || "";
      if (!ALLOWED.test(tabUrl) || !ALLOWED.test(msgUrl)) {
        sendResponse({ ok: false, error: "refusing: not from connect.raspberrypi.com tab" });
        return;
      }
      const url = tabUrl;
      const idOk = (id) => /^[\w-]{1,64}$/.test(id || "");
      const origin = new URL(url).origin;
      const cookies = await chrome.cookies.getAll({ url: origin + "/" });
      // least-privilege: session/csrf only; fail loudly instead of forwarding analytics
      const keep = cookies.filter(c => /session|csrf|remember/i.test(c.name))
        .filter(c => typeof c.name === "string" && typeof c.value === "string")
        .filter(c => !/[;\r\n]/.test(c.name + c.value));
      if (!keep.length) throw new Error("no session cookies - reload the Connect page and retry");
      if (typeof msg.payload.csrfToken !== "string" || /[\r\n]/.test(msg.payload.csrfToken)) {
        sendResponse({ ok: false, error: "bad csrfToken" }); return;
      }
      const header = keep.map(c => `${c.name}=${c.value}`).join("; ");
      const forward = (payload) => chrome.runtime.sendNativeMessage(HOST, payload, (resp) => {
        if (chrome.runtime.lastError) sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        else if (resp && resp.ok === false) sendResponse({ ok: false, error: resp.error || "native host failed" });
        else sendResponse({ ok: true, resp });
      });
      if (!msg || (msg.type !== "OPEN_ITERM2" && msg.type !== "OPEN_ITERM2_FROM_LIST") || !msg.payload) {
        sendResponse({ ok: false, error: "unknown message" });
        return;
      }
      const tmux = msg.payload.tmux === true;
      const tmuxSession = (typeof msg.payload.tmuxSession === "string" && /^[\w-]{1,32}$/.test(msg.payload.tmuxSession))
        ? msg.payload.tmuxSession : undefined;
      if (msg?.type === "OPEN_ITERM2") {
        if (!idOk(msg.payload.deviceId)) { sendResponse({ ok: false, error: "bad deviceId" }); return; }
        forward({ deviceId: msg.payload.deviceId, csrfToken: msg.payload.csrfToken,
          iceConfiguration: msg.payload.iceConfiguration, cookies: header, tmux, tmuxSession });
      } else if (msg?.type === "OPEN_ITERM2_FROM_LIST") {
        if (!idOk(msg.payload.deviceId)) { sendResponse({ ok: false, error: "bad deviceId" }); return; }
        const r = await fetch(`${origin}/devices/${encodeURIComponent(msg.payload.deviceId)}/ice-configuration`,
          { headers: { Accept: "application/json", "X-CSRF-Token": msg.payload.csrfToken || "" }, credentials: "include" });
        if (!r.ok) throw new Error(`ice-configuration -> ${r.status}`);
        forward({ deviceId: msg.payload.deviceId, csrfToken: msg.payload.csrfToken,
          iceConfiguration: await r.json(), cookies: header, tmux, tmuxSession });
      } else sendResponse({ ok: false, error: "unknown message" });
    } catch (e) {
      try { sendResponse({ ok: false, error: String(e.message || e) }); } catch (_) {}
    }
  })();
  return true;
});
