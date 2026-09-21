// v0.6: floating button ONLY on shell pages (single-device context).
// List pages get per-device buttons next to each row — never a floating one.
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
    const kind = iceOrNull ? "OPEN_ITERM2" : "OPEN_ITERM2_FROM_LIST";
    const payload = iceOrNull
      ? { deviceId, csrfToken: csrf(), iceConfiguration: iceOrNull }
      : { deviceId, csrfToken: csrf() };
    console.log("[rpi-iterm] launching", deviceId, kind);
    const res = await chrome.runtime.sendMessage({ type: kind, payload });
    console.log("[rpi-iterm] host replied", res);
    if (!res?.ok) alert("Native host error: " + (res?.error || "no response"));
  }
  // list page: id -> element to anchor the button to. Multiple selector
  // strategies because the device list markup varies; never guesses.
  function deviceRows() {
    const rows = new Map();
    const add = (id, el) => { if (id && el && !rows.has(id)) rows.set(id, el); };
    document.querySelectorAll("[data-device-id]").forEach(el =>
      add(el.dataset.deviceId, el.closest("li,article,div,section,tr") || el));
    document.querySelectorAll('a[href*="/devices/"]').forEach(a => {
      const m = (a.getAttribute("href") || "").match(/\/devices\/([\w-]{1,64})/);
      if (!m) return;
      add(m[1], a.closest("li,article,div,section,tr") || a);
    });
    return rows;
  }
  function ensure() {
    if (!document.body) return;
    const onShell = !!document.querySelector('[data-controller="shell"]');
    if (onShell) {
      if (!document.getElementById("rpi-open-iterm2")) {
        const s = shellVals();
        const b = document.createElement("button");
        b.id = "rpi-open-iterm2";
        b.textContent = "Open in iTerm2";
        b.title = location.href;
        b.style.cssText = "position:fixed;bottom:24px;right:24px;z-index:2147483647;padding:12px 18px;font-size:14px;font-weight:700;border-radius:10px;cursor:pointer;background:#fff;border:2px solid #000;";
        b.onclick = () => { const v = shellVals(); v?.device && launch(v.device, v.ice); };
        document.body.appendChild(b);
        console.log("[rpi-iterm] shell button injected, device=", s?.device);
      }
      return;
    }
    // list page: per-device buttons only, no floating button.
    const rows = deviceRows();
    if (!rows.size) {
      console.log("[rpi-iterm] list page: 0 device rows found (selectors: [data-device-id], a[href*=devices])");
      return;
    }
    rows.forEach((host, id) => {
      const anchor = host.tagName === "A" ? host : host.querySelector?.('a[href*="/devices/"]');
      const parent = anchor?.parentElement || host;
      if (parent.querySelector?.(":scope > .rpi-open-iterm2-row")) return;
      const b = document.createElement("button");
      b.className = "rpi-open-iterm2-row";
      b.type = "button";
      b.textContent = "Open in iTerm2";
      b.title = "Open " + id + " in iTerm2";
      b.style.cssText = "margin-left:8px;font-size:12px;text-decoration:underline;cursor:pointer;";
      b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); launch(id, null); };
      if (anchor?.nextSibling) parent.insertBefore(b, anchor.nextSibling);
      else parent.appendChild?.(b);
    });
  }
  new MutationObserver(ensure).observe(document.documentElement, { childList: true, subtree: true });
  ensure();
})();
