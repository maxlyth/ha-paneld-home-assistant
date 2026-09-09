const g = Object.freeze({
  title: "Panel Assistant",
  menu: "Open navigation",
  refresh: "Refresh",
  install: "Install using USB",
  connect: "Add or connect a panel",
  introduction: "Refresh to see the latest status received by Home Assistant.",
  empty: "No panels are connected yet.",
  loading: "Loading panels…",
  failed: "Panels could not be loaded. Try refreshing.",
  admin: "An administrator must open this page.",
  available: "Available",
  unavailable: "Unavailable or not loaded",
  version: "Installed version",
  warnings: "Warnings",
  diagnostics: "Diagnostics unavailable",
  settings: "Panel settings",
  truncated: "Only the first 200 panels are shown.",
  checklist: "Check setup",
  checklistLoading: "Checking setup…",
  checklistFailed: "Setup checks are unavailable on this panel.",
  checklistHelp: "Helper, Shizuku and WebView guidance only. Complete Android permissions and guided setup separately.",
  checklistNew: "Some checks require a newer integration."
}), q = Object.freeze({ "access.helper": "Panel helper", "access.shizuku": "Shizuku", "software.webview": "WebView" }), T = Object.freeze({ satisfied: "Ready", actionable: "Action needed", manual: "Manual setup needed", blocked: "Cannot proceed", degraded: "Needs attention", not_applicable: "Not needed" });
function M(n) {
  if (!n || !Array.isArray(n.panels) || n.panels.length > 200 || typeof n.truncated != "boolean") throw Error("invalid fleet");
  const t = /* @__PURE__ */ new Set();
  return { panels: n.panels.map((e) => {
    if (!e || typeof e.entry_id != "string" || !/^[a-zA-Z0-9_-]{1,64}$/.test(e.entry_id) || t.has(e.entry_id) || typeof e.name != "string" || e.name.length > 256 || typeof e.available != "boolean" || typeof e.status_available != "boolean" || !(e.version === null || typeof e.version == "string" && e.version.length <= 128) || !(e.warning_count === null || Number.isSafeInteger(e.warning_count) && e.warning_count >= 0)) throw Error("invalid fleet");
    if (!e.available && (e.version !== null || e.warning_count !== null || e.status_available)) throw Error("stale fleet");
    if (e.status_available !== (e.warning_count !== null)) throw Error("invalid status");
    return t.add(e.entry_id), { entry_id: e.entry_id, name: e.name, available: e.available, version: e.version, warning_count: e.warning_count, status_available: e.status_available };
  }), truncated: n.truncated };
}
async function W(n, t) {
  return M(await j(n, t, "/api/panel_assistant/fleet"));
}
async function j(n, t, a) {
  let e, s;
  const r = new Promise((l, c) => {
    s = () => c(Error("cancelled"));
  });
  t.addEventListener("abort", s, { once: !0 });
  try {
    if (t.aborted) throw Error("cancelled");
    return await Promise.race([r, (async () => {
      const l = await n.fetchWithAuth(a, { signal: t, cache: "no-store", redirect: "error" });
      if (t.aborted || l.status !== 200 || l.redirected || l.headers.get("content-type")?.split(";")[0].trim() !== "application/json") throw Error("invalid response");
      e = l.body.getReader();
      const c = new TextDecoder("utf-8", { fatal: !0 });
      let p = "", o = 0;
      for (; ; ) {
        const { done: h, value: f } = await e.read();
        if (t.aborted) throw Error("cancelled");
        if (h) break;
        if (o += f.byteLength, o > 524288) throw Error("excessive response");
        p += c.decode(f, { stream: !0 });
      }
      return JSON.parse(p + c.decode());
    })()]);
  } finally {
    t.removeEventListener("abort", s), e && e.cancel().catch(() => {
    });
  }
}
class F extends HTMLElement {
  #t;
  #n;
  #e;
  #o;
  #a = "loading";
  #i;
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>
      :host{display:block;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#212121);min-height:100%;font:inherit}
      header{display:flex;align-items:center;gap:12px;background:var(--app-header-background-color,var(--primary-color,#03a9f4));color:var(--app-header-text-color,#fff);padding:8px 16px}
      h1{font-size:1.25rem}main{max-width:1000px;margin:auto;padding:20px;box-sizing:border-box}
      .brand{display:flex;align-items:center;gap:8px}.brand img{width:108px;height:108px;flex:none}.brand p{margin:0}
      nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center}button,a{font:inherit;padding:12px;min-height:44px;box-sizing:border-box}
      button{cursor:pointer;color:inherit;background:transparent;border:1px solid var(--divider-color,#888);border-radius:6px}a{color:var(--primary-color,#0288d1)}
      #panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px;margin-top:20px}
      article{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:var(--ha-card-border-radius,12px);padding:16px;overflow-wrap:anywhere}h2{font-size:1.1rem}article a{display:inline-block}
    </style><header><button id="menu" aria-label=""></button><h1 data-message="title"></h1></header><main>
      <div class="brand"><img src="/panel_assistant/usb/icon.svg" width="108" height="108" alt=""><p data-message="introduction"></p></div><nav><button id="refresh" data-message="refresh"></button><a href="/ha-paneld-usb" data-message="install"></a><a href="/config/integrations/dashboard/add?domain=panel_assistant" data-message="connect"></a></nav>
      <p id="status" role="status" aria-live="polite"></p><section id="panels"></section></main>`;
    for (const a of this.shadowRoot.querySelectorAll("[data-message]")) a.textContent = g[a.dataset.message];
    const t = this.shadowRoot.querySelector("#menu");
    t.textContent = "☰", t.setAttribute("aria-label", g.menu), t.addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: !0, composed: !0 }))), this.shadowRoot.querySelector("#refresh").addEventListener("click", () => this.#r()), this.#s();
  }
  set hass(t) {
    const a = this.#t?.user?.id !== t?.user?.id || this.#t?.user?.is_admin !== t?.user?.is_admin || this.#t?.connection !== t?.connection || this.#t?.auth !== t?.auth;
    this.#t = t, a && this.#r();
  }
  connectedCallback() {
    this.#r();
  }
  disconnectedCallback() {
    this.#n?.abort(), this.#n = void 0, this.#e?.abort(), this.#e = void 0;
  }
  async #r() {
    if (this.#e?.abort(), this.#e = void 0, this.#n?.abort(), this.#n = void 0, this.#i = void 0, this.#a = this.#t?.user?.is_admin === !0 ? "loading" : "admin", this.#s(), !this.isConnected || this.#a === "admin") return;
    const t = new AbortController();
    this.#n = t;
    const a = setTimeout(() => t.abort(), 15e3);
    try {
      const e = await W(this.#t, t.signal);
      if (t !== this.#n) return;
      this.#i = e, this.#a = e.panels.length ? null : "empty";
    } catch {
      t === this.#n && (this.#a = "failed");
    } finally {
      clearTimeout(a), t === this.#n && (this.#n = void 0, this.#s());
    }
  }
  async #l(t, a) {
    this.#e?.abort(), this.#o && (this.#o.textContent = ""), this.#o = a;
    const e = new AbortController();
    this.#e = e;
    const s = setTimeout(() => e.abort(), 15e3);
    a.textContent = g.checklistLoading;
    try {
      const r = await j(this.#t, e.signal, `/api/panel_assistant/fleet/${encodeURIComponent(t)}/provisioning`);
      if (this.#e !== e) return;
      if (!r || !Array.isArray(r.items) || r.items.length > 32 || typeof r.needs_updated_client != "boolean") throw Error("invalid plan");
      const l = r.items.map((c) => {
        if (!Object.hasOwn(q, c.id) || !Object.hasOwn(T, c.status)) throw Error("invalid item");
        return `${q[c.id]}: ${T[c.status]}`;
      });
      a.textContent = [...l, r.needs_updated_client ? g.checklistNew : "", g.checklistHelp].filter(Boolean).join(`
`);
    } catch {
      this.#e === e && (a.textContent = g.checklistFailed);
    } finally {
      clearTimeout(s), this.#e === e && (this.#e = void 0);
    }
  }
  #s() {
    this.shadowRoot.querySelector("#status").textContent = this.#a ? g[this.#a] : this.#i?.truncated ? g.truncated : "", this.shadowRoot.querySelector("#refresh").disabled = this.#a === "admin" || this.#a === "loading";
    const t = this.shadowRoot.querySelector("#panels");
    t.replaceChildren();
    for (const a of this.#i?.panels ?? []) {
      const e = document.createElement("article"), s = (c, p) => {
        const o = document.createElement(c);
        return o.textContent = p, e.append(o), o;
      };
      s("h2", a.name), s("p", g[a.available ? "available" : "unavailable"]), a.version !== null && s("p", `${g.version}: ${a.version}`), a.status_available ? s("p", `${g.warnings}: ${a.warning_count}`) : a.available && s("p", g.diagnostics), s("a", g.settings).href = `/config/integrations/integration/panel_assistant#config_entry=${encodeURIComponent(a.entry_id)}`;
      const r = s("button", g.checklist);
      r.disabled = !a.available;
      const l = s("p", "");
      l.setAttribute("role", "status"), l.style.whiteSpace = "pre-line", r.addEventListener("click", () => this.#l(a.entry_id, l)), t.append(e);
    }
  }
}
customElements.get("panel-assistant-fleet") || customElements.define("panel-assistant-fleet", F);
const z = "/api/panel_assistant/usb/release", O = 64 * 1024 * 1024, D = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, V = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, G = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], w = (n, t) => typeof t == "string" && n.exec(t)?.[0] === t, $ = (n, t) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === t.length && t.every((a) => Object.hasOwn(n, a));
class A extends Error {
  constructor(t) {
    super(t), this.name = "HandoffError", this.code = t;
  }
}
function u(n, t = "invalid_response") {
  if (!n) throw new A(t);
}
function S(n, t, a = !1) {
  u(typeof n == "string" && n.length <= Math.ceil(t / 3) * 4 && w(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, n));
  const e = atob(n);
  return u(btoa(e) === n && e.length > 0 && (a ? e.length === t : e.length <= t)), Uint8Array.from(e, (s) => s.charCodeAt(0));
}
async function H(n, t, a, e = null) {
  u(n.status === 200 && !n.redirected && n.body);
  const s = n.headers.get("content-length");
  if (s !== null) {
    u(w(/0|[1-9][0-9]*/, s));
    const o = Number(s);
    u(Number.isSafeInteger(o) && o <= t && (e === null || o === e));
  }
  const r = n.body.getReader(), l = () => {
    r.cancel().catch(() => {
    });
  };
  a.addEventListener("abort", l, { once: !0 });
  const c = [];
  let p = 0;
  try {
    for (; ; ) {
      u(!a.aborted, "cancelled");
      const o = await r.read();
      if (u(!a.aborted, "cancelled"), o.done) break;
      p += o.value.byteLength, u(p <= t && (e === null || p <= e)), c.push(o.value);
    }
    return u(p > 0 && (s === null || p === Number(s)) && (e === null || p === e)), new Blob(c);
  } finally {
    a.removeEventListener("abort", l), l(), r.releaseLock();
  }
}
function J(n, t, {
  rcTag: a = null,
  onState: e = () => {
  },
  windowObject: s = window,
  timeoutMs: r = 3e5
} = {}) {
  let l, c;
  const p = new Promise((i, d) => {
    l = i, c = d;
  }), o = new AbortController();
  let h = !1, f, m, y, x, C = !1, R = !1;
  const _ = (i) => {
    try {
      e(i);
    } catch {
    }
  }, v = (i = null) => {
    h || (h = !0, o.abort(), clearTimeout(m), s.removeEventListener("message", L), _(i ?? "verified"), i ? c(new A(i)) : l());
  };
  async function U() {
    try {
      _("preparing"), u(!h, "cancelled");
      const i = await n.fetchWithAuth(z, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(a === null ? {} : { release_candidate: a }),
        redirect: "error",
        signal: o.signal
      });
      u(!h, "cancelled"), u(i.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const d = JSON.parse(await (await H(i, 8192, o.signal)).text());
      u(!h, "cancelled"), u($(d, G) && w(/[0-9a-f]{32}/, d.id) && typeof d.tag == "string" && d.tag.length <= 64 && (a === null ? w(D, d.tag) : d.tag === a) && w(/[0-9a-f]{64}/, d.apk_sha256) && Number.isSafeInteger(d.apk_size) && d.apk_size > 0 && d.apk_size <= O);
      const E = {
        tag: d.tag,
        checksum: S(d.checksum, 512),
        checksumSignature: S(d.checksum_signature, 256, !0),
        descriptor: S(d.descriptor, 4096),
        descriptorSignature: S(d.descriptor_signature, 256, !0)
      };
      _("downloading"), u(!h, "cancelled");
      const B = await n.fetchWithAuth(`${z}/${d.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: o.signal
      });
      u(!h, "cancelled");
      const I = await H(B, O, o.signal, d.apk_size);
      u(!h && !f.closed, "window_closed"), R = !0, f.postMessage({ type: "ha-paneld/usb-bundle", nonce: x, bundle: E, apk: I }, y), _("verifying");
    } catch (i) {
      v(i instanceof A ? i.code : "delivery_failed");
    }
  }
  function L(i) {
    h || i.source !== f || i.origin !== y || !$(i.data, ["type", "nonce"]) || i.data.nonce !== x || (i.data.type === "ha-paneld/usb-ready" && !C ? (C = !0, U()) : i.data.type === "ha-paneld/usb-verified" && R ? v() : i.data.type === "ha-paneld/usb-error" && v("verification_failed"));
  }
  try {
    u(n && typeof n.fetchWithAuth == "function" && (a === null || a.length <= 64 && w(V, a)) && Number.isSafeInteger(r) && r > 0 && r <= 3e5, "invalid_request");
    const i = new URL(t);
    u(!i.username && !i.password && !i.hash && (i.protocol === "https:" || i.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(i.hostname)), "invalid_destination"), y = i.origin;
    const d = new Uint8Array(16);
    s.crypto.getRandomValues(d), x = Array.from(d, (E) => E.toString(16).padStart(2, "0")).join(""), i.hash = new URLSearchParams({ ha_origin: s.location.origin, nonce: x, rc: a ?? "" }).toString(), s.addEventListener("message", L), f = s.open(i.href, "_blank"), u(f, "popup_blocked"), m = setTimeout(() => v("timeout"), r), _("waiting");
  } catch (i) {
    v(i instanceof A ? i.code : "invalid_request");
  }
  return { completion: p, cancel: () => v("cancelled") };
}
const Z = 30, N = 8192, X = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, K = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, P = (n, t) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === t.length && t.every((a) => Object.hasOwn(n, a));
function b(n) {
  if (!n) throw new Error("Invalid release catalogue");
}
function Q(n) {
  b(P(n, ["releases"]) && Array.isArray(n.releases) && n.releases.length <= Z);
  const t = /* @__PURE__ */ new Set();
  let a = 0;
  return Object.freeze(n.releases.map((e) => (b(P(e, ["tag", "prerelease"]) && typeof e.prerelease == "boolean" && typeof e.tag == "string" && e.tag.length <= 64 && (e.prerelease ? K : X).exec(e.tag)?.[0] === e.tag && !t.has(e.tag)), t.add(e.tag), e.prerelease || b(++a <= 1), Object.freeze({ tag: e.tag, prerelease: e.prerelease }))));
}
async function Y(n, { signal: t, timeoutMs: a = 15e3 } = {}) {
  const e = new AbortController(), s = () => e.abort();
  t?.addEventListener("abort", s, { once: !0 }), t?.aborted && s();
  const r = setTimeout(s, a);
  let l, c;
  const p = new Promise((o, h) => {
    c = () => h(new Error("Release catalogue cancelled"));
  });
  e.signal.addEventListener("abort", c, { once: !0 });
  try {
    return b(!e.signal.aborted), await Promise.race([p, (async () => {
      const o = await n.fetchWithAuth("/api/panel_assistant/usb/releases", {
        method: "GET",
        redirect: "error",
        signal: e.signal
      });
      b(!e.signal.aborted && o.status === 200 && !o.redirected && o.body && o.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const h = o.headers.get("content-length");
      b(h === null || /^(0|[1-9][0-9]*)$/.exec(h)?.[0] === h && Number(h) <= N), l = o.body.getReader();
      const f = [];
      let m = 0;
      for (; ; ) {
        const y = await l.read();
        if (b(!e.signal.aborted), y.done) break;
        m += y.value.byteLength, b(m <= N), f.push(y.value);
      }
      return b(m > 0 && (h === null || m === Number(h))), Q(JSON.parse(await new Blob(f).text()));
    })()]);
  } finally {
    clearTimeout(r), t?.removeEventListener("abort", s), e.signal.removeEventListener("abort", c), e.abort(), l && l.cancel().catch(() => {
    });
  }
}
const k = Object.freeze({
  title: "Panel Assistant USB installation",
  introduction: "Connect the panel to this browser’s computer or mobile device, not to the Home Assistant server. USB debugging and Android authorization are required.",
  scope: "This experimental installer supports clean installation only. Existing installations are not overwritten. Setup permissions and connecting the panel to Home Assistant remain separate steps. MQTT is unchanged.",
  release: "ha-paneld version",
  releaseHelp: "Stable is recommended. Release candidates are for testing. To resume, use the same release and browser as before.",
  loading: "Loading available versions…",
  catalogError: "Available versions could not be loaded. Try again.",
  empty: "No supported versions are available yet. Try again later.",
  choose: "Choose a version",
  retry: "Retry",
  start: "Open USB installer",
  cancel: "Cancel release transfer",
  ready: "A separate secure window will verify the release before asking you to select a USB panel. Nothing is installed without confirmation.",
  unavailable: "The secure installer location has not been configured for this integration.",
  admin: "An administrator must open the installer.",
  waiting: "Installer window opened. Waiting for it to become ready…",
  preparing: "Home Assistant is preparing the signed release…",
  downloading: "Downloading the verified release from Home Assistant…",
  verifying: "The installer window is independently verifying the release…",
  verified: "Release verified. Continue in the installer window to select the USB panel and review installation. This does not mean the panel is installed.",
  cancelled: "Release transfer cancelled. This does not cancel an installation already started in the other window.",
  popup_blocked: "Allow this Home Assistant page to open a popup, then try again.",
  invalid_request: "Choose an available version before opening the installer.",
  failed: "Release transfer did not complete. Check the installer window and try again. No installation result is implied."
});
class ee extends HTMLElement {
  #t;
  #n;
  #e;
  #o = "ready";
  #a;
  #i = "loading";
  #r = [];
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#17232d);padding:24px;box-sizing:border-box}
      main{max-width:680px;margin:auto;font:inherit;line-height:1.5}
      h1{font-size:1.6rem}label{display:block;font-weight:600}
      select{display:block;box-sizing:border-box;width:100%;max-width:26rem;padding:12px;font:inherit}
      button{padding:12px 18px;font:inherit;margin:8px 8px 8px 0;cursor:pointer}
      button:disabled{cursor:default}p{overflow-wrap:anywhere}
      #status{padding:16px;border:1px solid var(--divider-color,#aab6bd);border-radius:8px}
    </style><main>
      <h1 data-message="title"></h1><p data-message="introduction"></p>
      <p data-message="scope"></p>
      <label for="release" data-message="release"></label>
      <select id="release" aria-describedby="release-help catalog-status"></select>
      <p id="release-help" data-message="releaseHelp"></p>
      <p id="catalog-status" role="status" aria-live="polite"></p><button id="retry" data-message="retry"></button>
      <button id="start" data-message="start"></button><button id="cancel" data-message="cancel"></button>
      <p id="status" role="status" aria-live="polite"></p>
    </main>`;
    for (const t of this.shadowRoot.querySelectorAll("[data-message]"))
      t.textContent = k[t.dataset.message];
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#d()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.shadowRoot.querySelector("#retry").addEventListener("click", () => this.#l()), this.shadowRoot.querySelector("#release").addEventListener("change", () => this.#s()), this.#s();
  }
  set hass(t) {
    const a = this.#t?.user?.id !== t?.user?.id || this.#t?.user?.is_admin !== t?.user?.is_admin || this.#t?.connection !== t?.connection || this.#t?.auth !== t?.auth;
    this.#t = t, a && (this.#e?.cancel(), this.#l()), this.#s();
  }
  set panel(t) {
    const a = this.#n?.config?.installer_url !== t?.config?.installer_url;
    a && this.#e?.cancel(), this.#n = t, a && this.#l(), this.#s();
  }
  connectedCallback() {
    this.#l();
  }
  disconnectedCallback() {
    this.#e?.cancel(), this.#a?.abort(), this.#a = void 0;
  }
  async #l() {
    if (this.#a?.abort(), this.#a = void 0, this.#r = [], this.#i = "loading", this.shadowRoot.querySelector("#release").replaceChildren(), this.#s(), !this.isConnected || this.#t?.user?.is_admin !== !0 || !this.#n?.config?.installer_url) return;
    const t = new AbortController();
    this.#a = t;
    try {
      const a = await Y(this.#t, { signal: t.signal });
      if (this.#a !== t) return;
      this.#r = a, this.#i = a.length ? "ready" : "empty";
      const e = this.shadowRoot.querySelector("#release"), s = document.createElement("option");
      s.value = "", s.textContent = k.choose, s.disabled = !0, e.append(s);
      for (const r of a) {
        const l = document.createElement("option");
        l.value = r.tag, l.textContent = `${r.tag} — ${r.prerelease ? "Release candidate (testing)" : "Stable"}`, e.append(l);
      }
      e.value = a.find((r) => !r.prerelease)?.tag ?? "";
    } catch {
      if (this.#a !== t) return;
      this.#i = "catalogError";
    } finally {
      this.#a === t && (this.#a = void 0, this.#s());
    }
  }
  #s() {
    const t = this.#t?.user?.is_admin === !0, a = typeof this.#n?.config?.installer_url == "string" && this.#n.config.installer_url.length > 0, e = this.#r.find((r) => r.tag === this.shadowRoot.querySelector("#release").value);
    this.shadowRoot.querySelector("#start").disabled = !t || !a || !!this.#e || !e, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e || this.#i !== "ready", this.shadowRoot.querySelector("#catalog-status").textContent = t && a && this.#i !== "ready" ? k[this.#i] : "", this.shadowRoot.querySelector("#retry").hidden = !t || !a || !["catalogError", "empty"].includes(this.#i);
    const s = t ? a ? this.#o : "unavailable" : "admin";
    this.shadowRoot.querySelector("#status").textContent = k[s] ?? k.failed;
  }
  #d() {
    if (this.#e || this.#t?.user?.is_admin !== !0) return;
    const t = this.#r.find((e) => e.tag === this.shadowRoot.querySelector("#release").value);
    if (!t || !this.isConnected) return;
    const a = J(this.#t, this.#n?.config?.installer_url, {
      rcTag: t.prerelease ? t.tag : null,
      onState: (e) => {
        this.#o = e, this.#s();
      }
    });
    this.#e = a, this.#s(), a.completion.catch(() => {
    }).finally(() => {
      this.#e === a && (this.#e = void 0), this.#s();
    });
  }
}
customElements.get("panel-assistant-usb-install") || customElements.define("panel-assistant-usb-install", ee);
