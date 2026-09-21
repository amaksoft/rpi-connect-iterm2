// v0.5: shell page + per-row list buttons. Never guesses "first link".
(function () {
  console.log("[rpi-iterm] content script loaded on", location.href);

  function shellVals() {
    const el = document.querySelector('[data-controller="shell"]');
    if (!el) return null;
    try {
      return {
        device: JSON.parse(el.dataset.shellDeviceValue || "{}").id || "",
        ice: JSON.parse(el.dataset.shellIceConfigurationValue || "null")
      };
    } catch (e) { console.log("[rpi-iterm] parse error", e); return null; }
  }
  function csrf() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }
  async function launch(deviceId, iceOrNull) {
    if (iceOrNull) {
      console.log("[rpi-iterm] launching from shell data", deviceId);
      const res = await chrome.runtime.sendMessage({ type: "OPEN_ITERM2",
        payload: { deviceId, csrfToken: csrf(), iceConfiguration: iceOrNull } });
      console.log("[rpi-iterm] host replied", res);
      if (!res?.ok) alert("Native host error: " + (res?.error || "no response"));
      return;
    }
    const res = await chrome.runtime.sendMessage({ type: "OPEN_ITERM2_FROM_LIST",
      payload: { deviceId, csrfToken: csrf() } });
    console.log("[rpi-iterm] host replied", res);
    if (!res?.ok) alert("Native host error: " + (res?.error || "no response"));
  }
  async function openFloating() {
    const s = shellVals();
    if (s?.device && s?.ice) { launch(s.device, s.ice); return; }
    const ids = [...deviceRows().keys()];
    if (ids.length === 1) launch(ids[0], null);
    else alert(ids.length ? "Click the per-device button next to the Pi you want." : "No device found on this page. Open the remote-shell-session page first.");
  }
  // list page: one button per device card/row (never "first link on page")
  function deviceRows() {
    const rows = new Map();
    document.querySelectorAll('a[href*="/devices/"]').forEach(a => {
      const m = (a.href || "").match(/devices\/([\w-]{1,64})/);
      if (!m) return;
      if (!rows.has(m[1])) rows.set(m[1], a.closest("li,article,div,section,tr") || a);
    });
    return rows;
  }
  function ensure() {
    if (!document.body) return;
    const onShell = !!document.querySelector('[data-controller="shell"]');
    if (!document.getElementById("rpi-open-iterm2")) {
      const b = document.createElement("button");
      b.id = "rpi-open-iterm2";
      b.textContent = onShell ? "Open in iTerm2" : "Open in iTerm2";
      b.title = location.href;
      b.style.cssText = "position:fixed;bottom:24px;right:24px;z-index:2147483647;padding:12px 18px;font-size:14px;font-weight:700;border-radius:10px;cursor:pointer;background:#fff;border:2px solid #000;";
      b.onclick = openFloating;
      document.body.appendChild(b);
    }
    if (!onShell) {
      // per-row buttons carry their own device id — no guessing.
      // Never nest inside the anchor (unreliable nav-cancel); insert after it.
      deviceRows().forEach((host, id) => {
        const anchor = host.tagName === "A" ? host : host.querySelector?.('a[href*="/devices/"]');
        const parent = anchor?.parentElement || host;
        if (parent.querySelector?.(":scope > .rpi-open-iterm2-row")) return;
        const b = document.createElement("button");
        b.className = "rpi-open-iterm2-row";
        b.type = "button";
        b.textContent = "Open in iTerm2";
        b.style.cssText = "margin-left:8px;font-size:12px;text-decoration:underline;cursor:pointer;";
        b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); launch(id, null); };
        if (anchor?.nextSibling) parent.insertBefore(b, anchor.nextSibling);
        else parent.appendChild?.(b);
      });
    }
  }
  new MutationObserver(ensure).observe(document.documentElement, { childList: true, subtree: true });
  ensure();
})();
