const f = Object.freeze({
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
}), O = Object.freeze({ "access.helper": "Panel helper", "access.shizuku": "Shizuku", "software.webview": "WebView" }), R = Object.freeze({ satisfied: "Ready", actionable: "Action needed", manual: "Manual setup needed", blocked: "Cannot proceed", degraded: "Needs attention", not_applicable: "Not needed" });
function se(a) {
  if (!a || !Array.isArray(a.panels) || a.panels.length > 200 || typeof a.truncated != "boolean") throw Error("invalid fleet");
  const e = /* @__PURE__ */ new Set();
  return { panels: a.panels.map((t) => {
    if (!t || typeof t.entry_id != "string" || !/^[a-zA-Z0-9_-]{1,64}$/.test(t.entry_id) || e.has(t.entry_id) || typeof t.name != "string" || t.name.length > 256 || typeof t.available != "boolean" || typeof t.status_available != "boolean" || !(t.version === null || typeof t.version == "string" && t.version.length <= 128) || !(t.warning_count === null || Number.isSafeInteger(t.warning_count) && t.warning_count >= 0)) throw Error("invalid fleet");
    if (!t.available && (t.version !== null || t.warning_count !== null || t.status_available)) throw Error("stale fleet");
    if (t.status_available !== (t.warning_count !== null)) throw Error("invalid status");
    return e.add(t.entry_id), { entry_id: t.entry_id, name: t.name, available: t.available, version: t.version, warning_count: t.warning_count, status_available: t.status_available };
  }), truncated: a.truncated };
}
async function oe(a, e) {
  return se(await V(a, e, "/api/panel_assistant/fleet"));
}
async function V(a, e, n) {
  let t, r;
  const i = new Promise((s, l) => {
    r = () => l(Error("cancelled"));
  });
  e.addEventListener("abort", r, { once: !0 });
  try {
    if (e.aborted) throw Error("cancelled");
    return await Promise.race([i, (async () => {
      const s = await a.fetchWithAuth(n, { signal: e, cache: "no-store", redirect: "error" });
      if (e.aborted || s.status !== 200 || s.redirected || s.headers.get("content-type")?.split(";")[0].trim() !== "application/json") throw Error("invalid response");
      t = s.body.getReader();
      const l = new TextDecoder("utf-8", { fatal: !0 });
      let g = "", d = 0;
      for (; ; ) {
        const { done: h, value: p } = await t.read();
        if (e.aborted) throw Error("cancelled");
        if (h) break;
        if (d += p.byteLength, d > 524288) throw Error("excessive response");
        g += l.decode(p, { stream: !0 });
      }
      return JSON.parse(g + l.decode());
    })()]);
  } finally {
    e.removeEventListener("abort", r), t && t.cancel().catch(() => {
    });
  }
}
class de extends HTMLElement {
  #t;
  #a;
  #e;
  #s;
  #i = "loading";
  #n;
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
    for (const n of this.shadowRoot.querySelectorAll("[data-message]")) n.textContent = f[n.dataset.message];
    const e = this.shadowRoot.querySelector("#menu");
    e.textContent = "☰", e.setAttribute("aria-label", f.menu), e.addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: !0, composed: !0 }))), this.shadowRoot.querySelector("#refresh").addEventListener("click", () => this.#r()), this.#d();
  }
  set hass(e) {
    const n = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e, n && this.#r();
  }
  connectedCallback() {
    this.#r();
  }
  disconnectedCallback() {
    this.#a?.abort(), this.#a = void 0, this.#e?.abort(), this.#e = void 0;
  }
  async #r() {
    if (this.#e?.abort(), this.#e = void 0, this.#a?.abort(), this.#a = void 0, this.#n = void 0, this.#i = this.#t?.user?.is_admin === !0 ? "loading" : "admin", this.#d(), !this.isConnected || this.#i === "admin") return;
    const e = new AbortController();
    this.#a = e;
    const n = setTimeout(() => e.abort(), 15e3);
    try {
      const t = await oe(this.#t, e.signal);
      if (e !== this.#a) return;
      this.#n = t, this.#i = t.panels.length ? null : "empty";
    } catch {
      e === this.#a && (this.#i = "failed");
    } finally {
      clearTimeout(n), e === this.#a && (this.#a = void 0, this.#d());
    }
  }
  async #l(e, n) {
    this.#e?.abort(), this.#s && (this.#s.textContent = ""), this.#s = n;
    const t = new AbortController();
    this.#e = t;
    const r = setTimeout(() => t.abort(), 15e3);
    n.textContent = f.checklistLoading;
    try {
      const i = await V(this.#t, t.signal, `/api/panel_assistant/fleet/${encodeURIComponent(e)}/provisioning`);
      if (this.#e !== t) return;
      if (!i || !Array.isArray(i.items) || i.items.length > 32 || typeof i.needs_updated_client != "boolean") throw Error("invalid plan");
      const s = i.items.map((l) => {
        if (!Object.hasOwn(O, l.id) || !Object.hasOwn(R, l.status)) throw Error("invalid item");
        return `${O[l.id]}: ${R[l.status]}`;
      });
      n.textContent = [...s, i.needs_updated_client ? f.checklistNew : "", f.checklistHelp].filter(Boolean).join(`
`);
    } catch {
      this.#e === t && (n.textContent = f.checklistFailed);
    } finally {
      clearTimeout(r), this.#e === t && (this.#e = void 0);
    }
  }
  #d() {
    this.shadowRoot.querySelector("#status").textContent = this.#i ? f[this.#i] : this.#n?.truncated ? f.truncated : "", this.shadowRoot.querySelector("#refresh").disabled = this.#i === "admin" || this.#i === "loading";
    const e = this.shadowRoot.querySelector("#panels");
    e.replaceChildren();
    for (const n of this.#n?.panels ?? []) {
      const t = document.createElement("article"), r = (l, g) => {
        const d = document.createElement(l);
        return d.textContent = g, t.append(d), d;
      };
      r("h2", n.name), r("p", f[n.available ? "available" : "unavailable"]), n.version !== null && r("p", `${f.version}: ${n.version}`), n.status_available ? r("p", `${f.warnings}: ${n.warning_count}`) : n.available && r("p", f.diagnostics), r("a", f.settings).href = `/config/integrations/integration/panel_assistant#config_entry=${encodeURIComponent(n.entry_id)}`;
      const i = r("button", f.checklist);
      i.disabled = !n.available;
      const s = r("p", "");
      s.setAttribute("role", "status"), s.style.whiteSpace = "pre-line", i.addEventListener("click", () => this.#l(n.entry_id, s)), e.append(t);
    }
  }
}
customElements.get("panel-assistant-fleet") || customElements.define("panel-assistant-fleet", de);
const J = 64, Z = 256 * 1024, le = 2147483647, ce = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, he = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, U = /^build-([1-9][0-9]{0,9})$/, ue = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$/, D = (a, e) => typeof e == "string" && e.length <= J && a.exec(e)?.[0] === e, X = (a) => D(ce, a), K = (a) => D(he, a);
function ee(a) {
  if (!D(U, a)) return null;
  const e = Number(U.exec(a)[1]);
  return e <= le ? e : null;
}
const P = (a) => ee(a) !== null, ge = (a) => D(ue, a), pe = (a, e) => `${a} build ${e}`, Q = "/api/panel_assistant/usb/release", q = 64 * 1024 * 1024, fe = 1800 * 1e3, be = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], Me = ["id", "tag", "feed", "feed_signature", "apk_size", "apk_sha256"], te = 8192, ye = Math.ceil(Z / 3) * 4 + te, N = (a, e) => typeof e == "string" && a.exec(e)?.[0] === e, $ = (a, e) => a !== null && typeof a == "object" && !Array.isArray(a) && Object.keys(a).length === e.length && e.every((n) => Object.hasOwn(a, n));
class z extends Error {
  constructor(e) {
    super(e), this.name = "HandoffError", this.code = e;
  }
}
function u(a, e = "invalid_response") {
  if (!a) throw new z(e);
}
function A(a, e, n = !1) {
  u(typeof a == "string" && a.length <= Math.ceil(e / 3) * 4 && N(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, a));
  const t = atob(a);
  return u(btoa(t) === a && t.length > 0 && (n ? t.length === e : t.length <= e)), Uint8Array.from(t, (r) => r.charCodeAt(0));
}
async function Y(a, e, n, t = null) {
  u(a.status === 200 && !a.redirected && a.body);
  const r = a.headers.get("content-length");
  if (r !== null) {
    u(N(/0|[1-9][0-9]*/, r));
    const d = Number(r);
    u(Number.isSafeInteger(d) && d <= e && (t === null || d === t));
  }
  const i = a.body.getReader(), s = () => {
    i.cancel().catch(() => {
    });
  };
  n.addEventListener("abort", s, { once: !0 });
  const l = [];
  let g = 0;
  try {
    for (; ; ) {
      u(!n.aborted, "cancelled");
      const d = await i.read();
      if (u(!n.aborted, "cancelled"), d.done) break;
      g += d.value.byteLength, u(g <= e && (t === null || g <= t)), l.push(d.value);
    }
    return u(g > 0 && (r === null || g === Number(r)) && (t === null || g === t)), new Blob(l);
  } finally {
    n.removeEventListener("abort", s), s(), i.releaseLock();
  }
}
function me(a, e, {
  rcTag: n = null,
  onState: t = () => {
  },
  windowObject: r = window,
  timeoutMs: i = 3e5
} = {}) {
  let s, l;
  const g = new Promise((o, m) => {
    s = o, l = m;
  }), d = new AbortController();
  let h = !1, p, w, y, j, C = !1, T = !1, I, S, L;
  const E = () => {
    clearInterval(S), clearTimeout(L), I = void 0, r.removeEventListener("message", _);
  }, v = (o) => {
    try {
      t(o);
    } catch {
    }
  }, x = (o = null) => {
    if (!h) {
      if (h = !0, d.abort(), clearTimeout(w), v(o ?? "verified"), o) {
        E(), l(new z(o));
        return;
      }
      S = setInterval(() => {
        p.closed && E();
      }, 2e3), L = setTimeout(E, fe), s();
    }
  };
  async function ae() {
    try {
      v("preparing"), u(!h, "cancelled");
      const o = await a.fetchWithAuth(Q, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(n === null ? {} : { release_candidate: n }),
        redirect: "error",
        signal: d.signal
      });
      u(!h, "cancelled"), u(o.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const m = P(n), c = JSON.parse(await (await Y(
        o,
        m ? ye : te,
        d.signal
      )).text());
      u(!h, "cancelled"), u($(c, m ? Me : be) && N(/[0-9a-f]{32}/, c.id) && typeof c.tag == "string" && c.tag.length <= J && (n === null ? X(c.tag) : c.tag === n) && N(/[0-9a-f]{64}/, c.apk_sha256) && Number.isSafeInteger(c.apk_size) && c.apk_size > 0 && c.apk_size <= q);
      const ne = m ? {
        tag: c.tag,
        feed: A(c.feed, Z),
        feedSignature: A(c.feed_signature, 256, !0)
      } : {
        tag: c.tag,
        checksum: A(c.checksum, 512),
        checksumSignature: A(c.checksum_signature, 256, !0),
        descriptor: A(c.descriptor, 4096),
        descriptorSignature: A(c.descriptor_signature, 256, !0)
      };
      v("downloading"), u(!h, "cancelled");
      const ie = await a.fetchWithAuth(`${Q}/${c.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: d.signal
      });
      u(!h, "cancelled");
      const re = await Y(ie, q, d.signal, c.apk_size);
      u(!h && !p.closed, "window_closed"), T = !0, I = { type: "ha-paneld/usb-bundle", nonce: j, bundle: ne, apk: re }, p.postMessage(I, y), v("verifying");
    } catch (o) {
      x(o instanceof z ? o.code : "delivery_failed");
    }
  }
  function _(o) {
    if (!(o.source !== p || o.origin !== y || !$(o.data, ["type", "nonce"]) || o.data.nonce !== j)) {
      if (o.data.type === "ha-paneld/usb-ready") {
        !C && !h ? (C = !0, ae()) : I && !p.closed && p.postMessage(I, y);
        return;
      }
      h || (o.data.type === "ha-paneld/usb-verified" && T ? x() : o.data.type === "ha-paneld/usb-error" && x("verification_failed"));
    }
  }
  try {
    u(a && typeof a.fetchWithAuth == "function" && (n === null || K(n) || P(n)) && Number.isSafeInteger(i) && i > 0 && i <= 3e5, "invalid_request");
    const o = new URL(e);
    u(!o.username && !o.password && !o.hash && (o.protocol === "https:" || o.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(o.hostname)), "invalid_destination"), y = o.origin;
    const m = new Uint8Array(16);
    r.crypto.getRandomValues(m), j = Array.from(m, (c) => c.toString(16).padStart(2, "0")).join(""), o.hash = new URLSearchParams({ ha_origin: r.location.origin, nonce: j, rc: n ?? "" }).toString(), r.addEventListener("message", _), p = r.open(o.href, "_blank"), u(p, "popup_blocked"), w = setTimeout(() => x("timeout"), i), v("waiting");
  } catch (o) {
    x(o instanceof z ? o.code : "invalid_request");
  }
  return { completion: g, cancel: () => {
    x("cancelled"), E();
  } };
}
const B = 30, H = 500, G = 128 * 1024, k = (a, e) => a !== null && typeof a == "object" && !Array.isArray(a) && Object.keys(a).length === e.length && e.every((n) => Object.hasOwn(a, n));
function b(a) {
  if (!a) throw new Error("Invalid release catalogue");
}
function we(a) {
  const e = ee(a.tag), n = typeof a.name == "string" ? a.name.split(" ")[0] : null;
  return e !== null && a.prerelease === !0 && ge(n) && a.name === pe(n, e);
}
function xe(a) {
  b(k(a, ["releases"]) && Array.isArray(a.releases) && a.releases.length <= B + H);
  const e = /* @__PURE__ */ new Set();
  let n = 0, t = 0, r = 0;
  return Object.freeze(a.releases.map((i) => k(i, ["tag", "prerelease", "name"]) ? (b(we(i) && !e.has(i.tag) && ++r <= H), e.add(i.tag), Object.freeze({ tag: i.tag, prerelease: !0, name: i.name })) : (b(k(i, ["tag", "prerelease"]) && typeof i.prerelease == "boolean" && (i.prerelease ? K(i.tag) : X(i.tag)) && !e.has(i.tag) && ++t <= B), e.add(i.tag), i.prerelease || b(++n <= 1), Object.freeze({ tag: i.tag, prerelease: i.prerelease }))));
}
async function Ae(a, { signal: e, timeoutMs: n = 15e3 } = {}) {
  const t = new AbortController(), r = () => t.abort();
  e?.addEventListener("abort", r, { once: !0 }), e?.aborted && r();
  const i = setTimeout(r, n);
  let s, l;
  const g = new Promise((d, h) => {
    l = () => h(new Error("Release catalogue cancelled"));
  });
  t.signal.addEventListener("abort", l, { once: !0 });
  try {
    return b(!t.signal.aborted), await Promise.race([g, (async () => {
      const d = await a.fetchWithAuth("/api/panel_assistant/usb/releases", {
        method: "GET",
        redirect: "error",
        signal: t.signal
      });
      b(!t.signal.aborted && d.status === 200 && !d.redirected && d.body && d.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const h = d.headers.get("content-length");
      b(h === null || /^(0|[1-9][0-9]*)$/.exec(h)?.[0] === h && Number(h) <= G), s = d.body.getReader();
      const p = [];
      let w = 0;
      for (; ; ) {
        const y = await s.read();
        if (b(!t.signal.aborted), y.done) break;
        w += y.value.byteLength, b(w <= G), p.push(y.value);
      }
      return b(w > 0 && (h === null || w === Number(h))), xe(JSON.parse(await new Blob(p).text()));
    })()]);
  } finally {
    clearTimeout(i), e?.removeEventListener("abort", r), t.signal.removeEventListener("abort", l), t.abort(), s && s.cancel().catch(() => {
    });
  }
}
const Ie = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDgiIGhlaWdodD0iMTA4IiB2aWV3Qm94PSIwIDAgMTA4IDEwOCI+CjxwYXRoIGQ9Ik0yOCwzMiBoNTIgYTQsNCAwIDAgMSA0LDQgdjM3IGE0LDQgMCAwIDEgLTQsNCBoLTUyIGE0LDQgMCAwIDEgLTQsLTQgdi0zNyBhNCw0IDAgMCAxIDQsLTQgeiIgZmlsbD0iIzM3NDc0RiIvPgo8cGF0aCBkPSJNMjksMzUgaDUwIGEyLDIgMCAwIDEgMiwyIHYzNSBhMiwyIDAgMCAxIC0yLDIgaC01MCBhMiwyIDAgMCAxIC0yLC0yIHYtMzUgYTIsMiAwIDAgMSAyLC0yIHoiIGZpbGw9IiMwRTE2MjAiLz4KPGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoNDIuMDAsNDIuNTApIHNjYWxlKDAuMTAwMCkiPgo8cGF0aCBmaWxsPSIjRjJGNEY5IiBkPSJNMjQwIDIyNC44MTNDMjQwIDIzMy4wNjMgMjMzLjI1IDIzOS44MTMgMjI1IDIzOS44MTNIMTVDNi43NSAyMzkuODEzIDAgMjMzLjA2MyAwIDIyNC44MTNWMTM0LjgxM0MwIDEyNi41NjMgNC43NyAxMTUuMDQzIDEwLjYxIDEwOS4yMDNMMTA5LjM5IDEwLjQyM0MxMTUuMjIgNC41OTMwNCAxMjQuNzcgNC41OTMwNCAxMzAuNiAxMC40MjNMMjI5LjM5IDEwOS4yMTNDMjM1LjIyIDExNS4wNDMgMjQwIDEyNi41NzMgMjQwIDEzNC44MjNWMjI0LjgyM1YyMjQuODEzWiIvPgo8cGF0aCBmaWxsPSIjMThCQ0YyIiBkPSJNMjI5LjM5IDEwOS4yMDNMMTMwLjYxIDEwLjQyM0MxMjQuNzggNC41OTMwNCAxMTUuMjMgNC41OTMwNCAxMDkuNCAxMC40MjNMMTAuNjEgMTA5LjIwM0M0Ljc4IDExNS4wMzMgMCAxMjYuNTYzIDAgMTM0LjgxM1YyMjQuODEzQzAgMjMzLjA2MyA2Ljc1IDIzOS44MTMgMTUgMjM5LjgxM0gxMDcuMjdMNjYuNjQgMTk5LjE4M0M2NC41NSAxOTkuOTAzIDYyLjMyIDIwMC4zMTMgNjAgMjAwLjMxM0M0OC43IDIwMC4zMTMgMzkuNSAxOTEuMTEzIDM5LjUgMTc5LjgxM0MzOS41IDE2OC41MTMgNDguNyAxNTkuMzEzIDYwIDE1OS4zMTNDNzEuMyAxNTkuMzEzIDgwLjUgMTY4LjUxMyA4MC41IDE3OS44MTNDODAuNSAxODIuMTQzIDgwLjA5IDE4NC4zNzMgNzkuMzcgMTg2LjQ2M0wxMTEgMjE4LjA5M1YxMDIuMjEzQzEwNC4yIDk4Ljg3MyA5OS41IDkxLjg5MyA5OS41IDgzLjgyM0M5OS41IDcyLjUyMyAxMDguNyA2My4zMjMgMTIwIDYzLjMyM0MxMzEuMyA2My4zMjMgMTQwLjUgNzIuNTIzIDE0MC41IDgzLjgyM0MxNDAuNSA5MS44OTMgMTM1LjggOTguODczIDEyOSAxMDIuMjEzVjE4My40ODNMMTYwLjQ2IDE1Mi4wMjNDMTU5Ljg0IDE1MC4wNjMgMTU5LjUgMTQ3Ljk4MyAxNTkuNSAxNDUuODIzQzE1OS41IDEzNC41MjMgMTY4LjcgMTI1LjMyMyAxODAgMTI1LjMyM0MxOTEuMyAxMjUuMzIzIDIwMC41IDEzNC41MjMgMjAwLjUgMTQ1LjgyM0MyMDAuNSAxNTcuMTIzIDE5MS4zIDE2Ni4zMjMgMTgwIDE2Ni4zMjNDMTc3LjUgMTY2LjMyMyAxNzUuMTIgMTY1Ljg1MyAxNzIuOTEgMTY1LjAzM0wxMjkgMjA4Ljk0M1YyMzkuODIzSDIyNUMyMzMuMjUgMjM5LjgyMyAyNDAgMjMzLjA3MyAyNDAgMjI0LjgyM1YxMzQuODIzQzI0MCAxMjYuNTczIDIzNS4yMyAxMTUuMDUzIDIyOS4zOSAxMDkuMjEzVjEwOS4yMDNaIi8+CjwvZz4KPC9zdmc+Cg==", ve = Object.freeze(["Version", "Connect", "Install", "Set up"]);
function je(a) {
  return ve.map((e, n) => n < a ? `<li class="done">${e}</li>` : n === a ? `<li class="current" aria-current="step">${e}</li>` : `<li>${e}</li>`).join("");
}
const W = "--bg:#f2f3f5;--card:#fff;--card-head:#e7ebef;--card-border:#d9dde3;--divider:#e4e7ec;--input-bg:#fafbfc;--border:#c4cad2;--border-strong:#b6bec8;--text:#1b2430;--dim:#6a7480;--accent:#1e56a8;--ok:#3f7d49;--bad:#a02c20;--disabled-bg:#e2e5e9;--disabled-fg:#9aa3ad;--shadow:rgba(0,0,0,.18)", F = "--bg:#111;--card:#181818;--card-head:#222;--card-border:#242424;--divider:#2a2a2a;--input-bg:#161616;--border:#383838;--border-strong:#444;--text:#eee;--dim:#888;--accent:#9af;--ok:#8a8;--bad:#ffb3a6;--disabled-bg:#222;--disabled-fg:#666;--shadow:#000", Ee = `
:root,:host{color-scheme:light dark;${W};--primary:#2557a7;--primary-text:#fff;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root,:host{${F}}}
:host([theme=light]){color-scheme:light;${W}}
:host([theme=dark]){color-scheme:dark;${F}}
*,*::before,*::after{box-sizing:border-box}
.wiz{max-width:520px;margin:0 auto;padding:4px 0 24px;color:var(--text)}
.wiz-brand{display:flex;align-items:center;gap:.5em;font-size:1.3rem;font-weight:700;margin:0 0 14px}
.wiz-brand img{width:2.1em;height:2.1em;border-radius:6px;flex:none}
.wiz-dots{display:flex;gap:14px;justify-content:center;list-style:none;margin:4px 0 14px;padding:0;flex-wrap:wrap}
.wiz-dots li{display:flex;align-items:center;gap:6px;font-size:.8rem;color:var(--dim)}
.wiz-dots li::before{content:"";flex:none;width:.55rem;height:.55rem;border-radius:50%;background:var(--dim);opacity:.35}
.wiz-dots li.current{color:var(--text);font-weight:600}
.wiz-dots li.current::before{background:var(--primary);opacity:1}
.wiz-dots li.done::before{background:var(--ok);opacity:1}
.card{background:var(--card);border:1px solid var(--card-border);border-radius:12px;padding:14px 16px;margin:0 0 14px}
.card h2{margin:-14px -16px 12px;padding:9px 16px;background:var(--card-head);border-bottom:1px solid var(--divider);
  border-radius:12px 12px 0 0;font-size:1.15rem;line-height:1.35;color:var(--text)}
.card p{line-height:1.5;margin:2px 0 14px;color:var(--dim)}
.card p.lead{color:var(--text);font-size:1.05rem}
.card label{display:block;margin:14px 0 5px;font-weight:700;font-size:.92rem;color:var(--accent)}
.card select{display:block;width:100%;font:inherit;font-size:1rem;min-height:42px;padding:8px 10px;margin:0 0 6px;
  background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:6px}
button.primary,a.primary{display:block;width:100%;min-height:46px;margin-top:16px;padding:10px 16px;border:1px solid var(--primary);
  border-radius:8px;background:var(--primary);color:var(--primary-text);font:inherit;font-size:1rem;text-align:center;
  text-decoration:none;cursor:pointer}
button.secondary{display:block;width:100%;min-height:42px;margin-top:8px;padding:8px 16px;border:1px solid var(--border-strong);
  border-radius:8px;background:transparent;color:var(--accent);font:inherit;cursor:pointer}
button.primary:disabled,button.secondary:disabled{background:var(--disabled-bg);color:var(--disabled-fg);border-color:var(--divider);cursor:default}
.spinner{width:2.25rem;height:2.25rem;border-radius:50%;margin:.5rem 0 1.25rem;border:.25rem solid var(--divider);
  border-top-color:var(--primary);animation:wiz-spin .9s linear infinite}
@keyframes wiz-spin{to{transform:rotate(360deg)}}
.bar{height:.5rem;border-radius:1rem;background:var(--divider);overflow:hidden;margin:1rem 0 .9rem}
.bar>div{height:100%;width:0;background:var(--primary);transition:width .4s ease}
@media (prefers-reduced-motion:reduce){.spinner{animation-duration:3s}.bar>div{transition:none}}
.error h2{color:var(--bad)}
[hidden]{display:none!important}
`, M = Object.freeze({
  title: "Install ha-paneld on a panel",
  introduction: "Plug the panel into this computer with a USB cable. A new window will find it and install the app.",
  release: "Version",
  loading: "Loading versions…",
  catalogError: "The list of versions couldn’t be loaded.",
  empty: "No versions are available yet. Try again later.",
  choose: "Choose a version",
  recommended: "recommended",
  testing: "test version",
  devBuild: "dev build",
  retry: "Try again",
  start: "Continue",
  cancel: "Cancel",
  ready: "",
  unavailable: "The installer isn’t available. Update Panel Assistant, then try again.",
  admin: "Ask a Home Assistant administrator to install panels.",
  waiting: "Continue in the new window.",
  preparing: "Getting the app ready…",
  downloading: "Getting the app ready…",
  verifying: "Getting the app ready…",
  verified: "Continue in the new window.",
  cancelled: "Cancelled.",
  popup_blocked: "Your browser blocked the new window. Allow pop-ups for this page, then press Continue.",
  invalid_request: "Choose a version first.",
  failed: "That didn’t work. Press Continue to try again."
});
class ze extends HTMLElement {
  #t;
  #a;
  #e;
  // A finished transfer keeps answering a reloaded installer window until this
  // page goes away or a new transfer starts.
  #s;
  #i = "ready";
  #n;
  #r = "loading";
  #l = [];
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>${Ee}
      :host{display:block;min-height:100%;background:var(--bg);padding:24px 16px}
      .card p.status{color:var(--text);margin:14px 0 0}
    </style><main class="wiz">
      <div class="wiz-brand"><img src="${Ie}" alt=""><span>ha-paneld</span></div>
      <ol class="wiz-dots" aria-label="Progress">${je(0)}</ol>
      <section class="card">
        <h2 data-message="title"></h2>
        <p class="lead" data-message="introduction"></p>
        <label for="release" data-message="release"></label>
        <select id="release" aria-describedby="catalog-status"></select>
        <p id="catalog-status" role="status" aria-live="polite"></p>
        <button id="retry" class="secondary" data-message="retry"></button>
        <button id="start" class="primary" data-message="start"></button>
        <p id="status" class="status" role="status" aria-live="polite"></p>
        <button id="cancel" class="secondary" data-message="cancel"></button>
      </section>
    </main>`;
    for (const e of this.shadowRoot.querySelectorAll("[data-message]"))
      e.textContent = M[e.dataset.message];
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#c()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.shadowRoot.querySelector("#retry").addEventListener("click", () => this.#d()), this.shadowRoot.querySelector("#release").addEventListener("change", () => this.#o()), this.#o();
  }
  set hass(e) {
    const n = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e;
    const t = e?.themes?.darkMode;
    typeof t == "boolean" && this.setAttribute?.("theme", t ? "dark" : "light"), n && (this.#e?.cancel(), this.#d()), this.#o();
  }
  set panel(e) {
    const n = this.#a?.config?.installer_url !== e?.config?.installer_url;
    n && this.#e?.cancel(), this.#a = e, n && this.#d(), this.#o();
  }
  connectedCallback() {
    this.#d();
  }
  disconnectedCallback() {
    this.#e?.cancel(), this.#s?.cancel(), this.#s = void 0, this.#n?.abort(), this.#n = void 0;
  }
  async #d() {
    if (this.#n?.abort(), this.#n = void 0, this.#l = [], this.#r = "loading", this.shadowRoot.querySelector("#release").replaceChildren(), this.#o(), !this.isConnected || this.#t?.user?.is_admin !== !0 || !this.#a?.config?.installer_url) return;
    const e = new AbortController();
    this.#n = e;
    try {
      const n = await Ae(this.#t, { signal: e.signal });
      if (this.#n !== e) return;
      this.#l = n, this.#r = n.length ? "ready" : "empty";
      const t = this.shadowRoot.querySelector("#release"), r = document.createElement("option");
      r.value = "", r.textContent = M.choose, r.disabled = !0, t.append(r);
      const i = n.find((s) => !s.prerelease)?.tag ?? "";
      for (const s of n) {
        const l = document.createElement("option");
        l.value = s.tag;
        const g = s.tag === i ? M.recommended : s.name ? M.devBuild : s.prerelease ? M.testing : "";
        l.textContent = `${s.name ?? s.tag.replace(/^v/, "")}${g ? ` (${g})` : ""}`, t.append(l);
      }
      t.value = i;
    } catch {
      if (this.#n !== e) return;
      this.#r = "catalogError";
    } finally {
      this.#n === e && (this.#n = void 0, this.#o());
    }
  }
  #o() {
    const e = this.#t?.user?.is_admin === !0, n = typeof this.#a?.config?.installer_url == "string" && this.#a.config.installer_url.length > 0, t = this.#l.find((l) => l.tag === this.shadowRoot.querySelector("#release").value);
    this.shadowRoot.querySelector("#start").disabled = !e || !n || !!this.#e || !t, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e || this.#r !== "ready";
    const r = this.shadowRoot.querySelector("#catalog-status");
    r.textContent = e && n && this.#r !== "ready" ? M[this.#r] : "", r.hidden = !r.textContent, this.shadowRoot.querySelector("#retry").hidden = !e || !n || !["catalogError", "empty"].includes(this.#r), this.shadowRoot.querySelector("#cancel").hidden = !this.#e;
    const i = e ? n ? this.#i : "unavailable" : "admin", s = this.shadowRoot.querySelector("#status");
    s.textContent = Object.hasOwn(M, i) ? M[i] : M.failed, s.hidden = !s.textContent;
  }
  #c() {
    if (this.#e || this.#t?.user?.is_admin !== !0) return;
    const e = this.#l.find((t) => t.tag === this.shadowRoot.querySelector("#release").value);
    if (!e || !this.isConnected) return;
    this.#s?.cancel(), this.#s = void 0;
    const n = me(this.#t, this.#a?.config?.installer_url, {
      rcTag: e.prerelease ? e.tag : null,
      onState: (t) => {
        this.#i = t, this.#o();
      }
    });
    this.#e = n, this.#o(), n.completion.then(() => {
      this.#e === n && (this.#s = n);
    }, () => {
    }).finally(() => {
      this.#e === n && (this.#e = void 0), this.#o();
    });
  }
}
customElements.get("panel-assistant-usb-install") || customElements.define("panel-assistant-usb-install", ze);
