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
  truncated: "Only the first 200 panels are shown."
});
function I(n) {
  if (!n || !Array.isArray(n.panels) || n.panels.length > 200 || typeof n.truncated != "boolean") throw Error("invalid fleet");
  const e = /* @__PURE__ */ new Set();
  return { panels: n.panels.map((t) => {
    if (!t || typeof t.entry_id != "string" || !/^[a-zA-Z0-9_-]{1,64}$/.test(t.entry_id) || e.has(t.entry_id) || typeof t.name != "string" || t.name.length > 256 || typeof t.available != "boolean" || typeof t.status_available != "boolean" || !(t.version === null || typeof t.version == "string" && t.version.length <= 128) || !(t.warning_count === null || Number.isSafeInteger(t.warning_count) && t.warning_count >= 0)) throw Error("invalid fleet");
    if (!t.available && (t.version !== null || t.warning_count !== null || t.status_available)) throw Error("stale fleet");
    if (t.status_available !== (t.warning_count !== null)) throw Error("invalid status");
    return e.add(t.entry_id), { entry_id: t.entry_id, name: t.name, available: t.available, version: t.version, warning_count: t.warning_count, status_available: t.status_available };
  }), truncated: n.truncated };
}
async function j(n, e) {
  let a, t;
  const s = new Promise((i, l) => {
    t = () => l(Error("cancelled"));
  });
  e.addEventListener("abort", t, { once: !0 });
  try {
    if (e.aborted) throw Error("cancelled");
    return await Promise.race([s, (async () => {
      const i = await n.fetchWithAuth("/api/ha_paneld/fleet", { signal: e, cache: "no-store", redirect: "error" });
      if (e.aborted || i.status !== 200 || i.redirected || i.headers.get("content-type")?.split(";")[0].trim() !== "application/json") throw Error("invalid response");
      a = i.body.getReader();
      const l = new TextDecoder("utf-8", { fatal: !0 });
      let u = "", p = 0;
      for (; ; ) {
        const { done: o, value: c } = await a.read();
        if (e.aborted) throw Error("cancelled");
        if (o) break;
        if (p += c.byteLength, p > 524288) throw Error("excessive response");
        u += l.decode(c, { stream: !0 });
      }
      return I(JSON.parse(u + l.decode()));
    })()]);
  } finally {
    e.removeEventListener("abort", t), a && a.cancel().catch(() => {
    });
  }
}
class U extends HTMLElement {
  #t;
  #a;
  #e = "loading";
  #i;
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>
      :host{display:block;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#212121);min-height:100%;font:inherit}
      header{display:flex;align-items:center;gap:12px;background:var(--app-header-background-color,var(--primary-color,#03a9f4));color:var(--app-header-text-color,#fff);padding:8px 16px}
      h1{font-size:1.25rem}main{max-width:1000px;margin:auto;padding:20px;box-sizing:border-box}
      nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center}button,a{font:inherit;padding:12px;min-height:44px;box-sizing:border-box}
      button{cursor:pointer;color:inherit;background:transparent;border:1px solid var(--divider-color,#888);border-radius:6px}a{color:var(--primary-color,#0288d1)}
      #panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px;margin-top:20px}
      article{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:var(--ha-card-border-radius,12px);padding:16px;overflow-wrap:anywhere}h2{font-size:1.1rem}article a{display:inline-block}
    </style><header><button id="menu" aria-label=""></button><h1 data-message="title"></h1></header><main>
      <p data-message="introduction"></p><nav><button id="refresh" data-message="refresh"></button><a href="/ha-paneld-usb" data-message="install"></a><a href="/config/integrations/dashboard/add?domain=ha_paneld" data-message="connect"></a></nav>
      <p id="status" role="status" aria-live="polite"></p><section id="panels"></section></main>`;
    for (const a of this.shadowRoot.querySelectorAll("[data-message]")) a.textContent = g[a.dataset.message];
    const e = this.shadowRoot.querySelector("#menu");
    e.textContent = "☰", e.setAttribute("aria-label", g.menu), e.addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: !0, composed: !0 }))), this.shadowRoot.querySelector("#refresh").addEventListener("click", () => this.#n()), this.#s();
  }
  set hass(e) {
    const a = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e, a && this.#n();
  }
  connectedCallback() {
    this.#n();
  }
  disconnectedCallback() {
    this.#a?.abort(), this.#a = void 0;
  }
  async #n() {
    if (this.#a?.abort(), this.#a = void 0, this.#i = void 0, this.#e = this.#t?.user?.is_admin === !0 ? "loading" : "admin", this.#s(), !this.isConnected || this.#e === "admin") return;
    const e = new AbortController();
    this.#a = e;
    const a = setTimeout(() => e.abort(), 15e3);
    try {
      const t = await j(this.#t, e.signal);
      if (e !== this.#a) return;
      this.#i = t, this.#e = t.panels.length ? null : "empty";
    } catch {
      e === this.#a && (this.#e = "failed");
    } finally {
      clearTimeout(a), e === this.#a && (this.#a = void 0, this.#s());
    }
  }
  #s() {
    this.shadowRoot.querySelector("#status").textContent = this.#e ? g[this.#e] : this.#i?.truncated ? g.truncated : "", this.shadowRoot.querySelector("#refresh").disabled = this.#e === "admin" || this.#e === "loading";
    const e = this.shadowRoot.querySelector("#panels");
    e.replaceChildren();
    for (const a of this.#i?.panels ?? []) {
      const t = document.createElement("article"), s = (i, l) => {
        const u = document.createElement(i);
        return u.textContent = l, t.append(u), u;
      };
      s("h2", a.name), s("p", g[a.available ? "available" : "unavailable"]), a.version !== null && s("p", `${g.version}: ${a.version}`), a.status_available ? s("p", `${g.warnings}: ${a.warning_count}`) : a.available && s("p", g.diagnostics), s("a", g.settings).href = `/config/integrations/integration/ha_paneld#config_entry=${encodeURIComponent(a.entry_id)}`, e.append(t);
    }
  }
}
customElements.get("panel-assistant-fleet") || customElements.define("panel-assistant-fleet", U);
const q = "/api/ha_paneld/usb/release", T = 64 * 1024 * 1024, M = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, W = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, F = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], w = (n, e) => typeof e == "string" && n.exec(e)?.[0] === e, z = (n, e) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === e.length && e.every((a) => Object.hasOwn(n, a));
class S extends Error {
  constructor(e) {
    super(e), this.name = "HandoffError", this.code = e;
  }
}
function h(n, e = "invalid_response") {
  if (!n) throw new S(e);
}
function A(n, e, a = !1) {
  h(typeof n == "string" && n.length <= Math.ceil(e / 3) * 4 && w(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, n));
  const t = atob(n);
  return h(btoa(t) === n && t.length > 0 && (a ? t.length === e : t.length <= e)), Uint8Array.from(t, (s) => s.charCodeAt(0));
}
async function $(n, e, a, t = null) {
  h(n.status === 200 && !n.redirected && n.body);
  const s = n.headers.get("content-length");
  if (s !== null) {
    h(w(/0|[1-9][0-9]*/, s));
    const o = Number(s);
    h(Number.isSafeInteger(o) && o <= e && (t === null || o === t));
  }
  const i = n.body.getReader(), l = () => {
    i.cancel().catch(() => {
    });
  };
  a.addEventListener("abort", l, { once: !0 });
  const u = [];
  let p = 0;
  try {
    for (; ; ) {
      h(!a.aborted, "cancelled");
      const o = await i.read();
      if (h(!a.aborted, "cancelled"), o.done) break;
      p += o.value.byteLength, h(p <= e && (t === null || p <= t)), u.push(o.value);
    }
    return h(p > 0 && (s === null || p === Number(s)) && (t === null || p === t)), new Blob(u);
  } finally {
    a.removeEventListener("abort", l), l(), i.releaseLock();
  }
}
function D(n, e, {
  rcTag: a = null,
  onState: t = () => {
  },
  windowObject: s = window,
  timeoutMs: i = 3e5
} = {}) {
  let l, u;
  const p = new Promise((r, d) => {
    l = r, u = d;
  }), o = new AbortController();
  let c = !1, b, m, y, E, R = !1, C = !1;
  const _ = (r) => {
    try {
      t(r);
    } catch {
    }
  }, v = (r = null) => {
    c || (c = !0, o.abort(), clearTimeout(m), s.removeEventListener("message", L), _(r ?? "verified"), r ? u(new S(r)) : l());
  };
  async function O() {
    try {
      _("preparing"), h(!c, "cancelled");
      const r = await n.fetchWithAuth(q, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(a === null ? {} : { release_candidate: a }),
        redirect: "error",
        signal: o.signal
      });
      h(!c, "cancelled"), h(r.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const d = JSON.parse(await (await $(r, 8192, o.signal)).text());
      h(!c, "cancelled"), h(z(d, F) && w(/[0-9a-f]{32}/, d.id) && typeof d.tag == "string" && d.tag.length <= 64 && (a === null ? w(M, d.tag) : d.tag === a) && w(/[0-9a-f]{64}/, d.apk_sha256) && Number.isSafeInteger(d.apk_size) && d.apk_size > 0 && d.apk_size <= T);
      const k = {
        tag: d.tag,
        checksum: A(d.checksum, 512),
        checksumSignature: A(d.checksum_signature, 256, !0),
        descriptor: A(d.descriptor, 4096),
        descriptorSignature: A(d.descriptor_signature, 256, !0)
      };
      _("downloading"), h(!c, "cancelled");
      const P = await n.fetchWithAuth(`${q}/${d.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: o.signal
      });
      h(!c, "cancelled");
      const B = await $(P, T, o.signal, d.apk_size);
      h(!c && !b.closed, "window_closed"), C = !0, b.postMessage({ type: "ha-paneld/usb-bundle", nonce: E, bundle: k, apk: B }, y), _("verifying");
    } catch (r) {
      v(r instanceof S ? r.code : "delivery_failed");
    }
  }
  function L(r) {
    c || r.source !== b || r.origin !== y || !z(r.data, ["type", "nonce"]) || r.data.nonce !== E || (r.data.type === "ha-paneld/usb-ready" && !R ? (R = !0, O()) : r.data.type === "ha-paneld/usb-verified" && C ? v() : r.data.type === "ha-paneld/usb-error" && v("verification_failed"));
  }
  try {
    h(n && typeof n.fetchWithAuth == "function" && (a === null || a.length <= 64 && w(W, a)) && Number.isSafeInteger(i) && i > 0 && i <= 3e5, "invalid_request");
    const r = new URL(e);
    h(!r.username && !r.password && !r.hash && (r.protocol === "https:" || r.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(r.hostname)), "invalid_destination"), y = r.origin;
    const d = new Uint8Array(16);
    s.crypto.getRandomValues(d), E = Array.from(d, (k) => k.toString(16).padStart(2, "0")).join(""), r.hash = new URLSearchParams({ ha_origin: s.location.origin, nonce: E, rc: a ?? "" }).toString(), s.addEventListener("message", L), b = s.open(r.href, "_blank"), h(b, "popup_blocked"), m = setTimeout(() => v("timeout"), i), _("waiting");
  } catch (r) {
    v(r instanceof S ? r.code : "invalid_request");
  }
  return { completion: p, cancel: () => v("cancelled") };
}
const G = 30, H = 8192, J = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, Z = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, N = (n, e) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === e.length && e.every((a) => Object.hasOwn(n, a));
function f(n) {
  if (!n) throw new Error("Invalid release catalogue");
}
function V(n) {
  f(N(n, ["releases"]) && Array.isArray(n.releases) && n.releases.length <= G);
  const e = /* @__PURE__ */ new Set();
  let a = 0;
  return Object.freeze(n.releases.map((t) => (f(N(t, ["tag", "prerelease"]) && typeof t.prerelease == "boolean" && typeof t.tag == "string" && t.tag.length <= 64 && (t.prerelease ? Z : J).exec(t.tag)?.[0] === t.tag && !e.has(t.tag)), e.add(t.tag), t.prerelease || f(++a <= 1), Object.freeze({ tag: t.tag, prerelease: t.prerelease }))));
}
async function X(n, { signal: e, timeoutMs: a = 15e3 } = {}) {
  const t = new AbortController(), s = () => t.abort();
  e?.addEventListener("abort", s, { once: !0 }), e?.aborted && s();
  const i = setTimeout(s, a);
  let l, u;
  const p = new Promise((o, c) => {
    u = () => c(new Error("Release catalogue cancelled"));
  });
  t.signal.addEventListener("abort", u, { once: !0 });
  try {
    return f(!t.signal.aborted), await Promise.race([p, (async () => {
      const o = await n.fetchWithAuth("/api/ha_paneld/usb/releases", {
        method: "GET",
        redirect: "error",
        signal: t.signal
      });
      f(!t.signal.aborted && o.status === 200 && !o.redirected && o.body && o.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const c = o.headers.get("content-length");
      f(c === null || /^(0|[1-9][0-9]*)$/.exec(c)?.[0] === c && Number(c) <= H), l = o.body.getReader();
      const b = [];
      let m = 0;
      for (; ; ) {
        const y = await l.read();
        if (f(!t.signal.aborted), y.done) break;
        m += y.value.byteLength, f(m <= H), b.push(y.value);
      }
      return f(m > 0 && (c === null || m === Number(c))), V(JSON.parse(await new Blob(b).text()));
    })()]);
  } finally {
    clearTimeout(i), e?.removeEventListener("abort", s), t.signal.removeEventListener("abort", u), t.abort(), l && l.cancel().catch(() => {
    });
  }
}
const x = Object.freeze({
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
class K extends HTMLElement {
  #t;
  #a;
  #e;
  #i = "ready";
  #n;
  #s = "loading";
  #o = [];
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
    for (const e of this.shadowRoot.querySelectorAll("[data-message]"))
      e.textContent = x[e.dataset.message];
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#d()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.shadowRoot.querySelector("#retry").addEventListener("click", () => this.#l()), this.shadowRoot.querySelector("#release").addEventListener("change", () => this.#r()), this.#r();
  }
  set hass(e) {
    const a = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e, a && (this.#e?.cancel(), this.#l()), this.#r();
  }
  set panel(e) {
    const a = this.#a?.config?.installer_url !== e?.config?.installer_url;
    a && this.#e?.cancel(), this.#a = e, a && this.#l(), this.#r();
  }
  connectedCallback() {
    this.#l();
  }
  disconnectedCallback() {
    this.#e?.cancel(), this.#n?.abort(), this.#n = void 0;
  }
  async #l() {
    if (this.#n?.abort(), this.#n = void 0, this.#o = [], this.#s = "loading", this.shadowRoot.querySelector("#release").replaceChildren(), this.#r(), !this.isConnected || this.#t?.user?.is_admin !== !0 || !this.#a?.config?.installer_url) return;
    const e = new AbortController();
    this.#n = e;
    try {
      const a = await X(this.#t, { signal: e.signal });
      if (this.#n !== e) return;
      this.#o = a, this.#s = a.length ? "ready" : "empty";
      const t = this.shadowRoot.querySelector("#release"), s = document.createElement("option");
      s.value = "", s.textContent = x.choose, s.disabled = !0, t.append(s);
      for (const i of a) {
        const l = document.createElement("option");
        l.value = i.tag, l.textContent = `${i.tag} — ${i.prerelease ? "Release candidate (testing)" : "Stable"}`, t.append(l);
      }
      t.value = a.find((i) => !i.prerelease)?.tag ?? "";
    } catch {
      if (this.#n !== e) return;
      this.#s = "catalogError";
    } finally {
      this.#n === e && (this.#n = void 0, this.#r());
    }
  }
  #r() {
    const e = this.#t?.user?.is_admin === !0, a = typeof this.#a?.config?.installer_url == "string" && this.#a.config.installer_url.length > 0, t = this.#o.find((i) => i.tag === this.shadowRoot.querySelector("#release").value);
    this.shadowRoot.querySelector("#start").disabled = !e || !a || !!this.#e || !t, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e || this.#s !== "ready", this.shadowRoot.querySelector("#catalog-status").textContent = e && a && this.#s !== "ready" ? x[this.#s] : "", this.shadowRoot.querySelector("#retry").hidden = !e || !a || !["catalogError", "empty"].includes(this.#s);
    const s = e ? a ? this.#i : "unavailable" : "admin";
    this.shadowRoot.querySelector("#status").textContent = x[s] ?? x.failed;
  }
  #d() {
    if (this.#e || this.#t?.user?.is_admin !== !0) return;
    const e = this.#o.find((t) => t.tag === this.shadowRoot.querySelector("#release").value);
    if (!e || !this.isConnected) return;
    const a = D(this.#t, this.#a?.config?.installer_url, {
      rcTag: e.prerelease ? e.tag : null,
      onState: (t) => {
        this.#i = t, this.#r();
      }
    });
    this.#e = a, this.#r(), a.completion.catch(() => {
    }).finally(() => {
      this.#e === a && (this.#e = void 0), this.#r();
    });
  }
}
customElements.get("ha-paneld-usb-install") || customElements.define("ha-paneld-usb-install", K);
