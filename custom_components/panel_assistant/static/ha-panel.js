const b = Object.freeze({
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
}), L = Object.freeze({ "access.helper": "Panel helper", "access.shizuku": "Shizuku", "software.webview": "WebView" }), _ = Object.freeze({ satisfied: "Ready", actionable: "Action needed", manual: "Manual setup needed", blocked: "Cannot proceed", degraded: "Needs attention", not_applicable: "Not needed" });
function J(n) {
  if (!n || !Array.isArray(n.panels) || n.panels.length > 200 || typeof n.truncated != "boolean") throw Error("invalid fleet");
  const e = /* @__PURE__ */ new Set();
  return { panels: n.panels.map((t) => {
    if (!t || typeof t.entry_id != "string" || !/^[a-zA-Z0-9_-]{1,64}$/.test(t.entry_id) || e.has(t.entry_id) || typeof t.name != "string" || t.name.length > 256 || typeof t.available != "boolean" || typeof t.status_available != "boolean" || !(t.version === null || typeof t.version == "string" && t.version.length <= 128) || !(t.warning_count === null || Number.isSafeInteger(t.warning_count) && t.warning_count >= 0)) throw Error("invalid fleet");
    if (!t.available && (t.version !== null || t.warning_count !== null || t.status_available)) throw Error("stale fleet");
    if (t.status_available !== (t.warning_count !== null)) throw Error("invalid status");
    return e.add(t.entry_id), { entry_id: t.entry_id, name: t.name, available: t.available, version: t.version, warning_count: t.warning_count, status_available: t.status_available };
  }), truncated: n.truncated };
}
async function V(n, e) {
  return J(await H(n, e, "/api/panel_assistant/fleet"));
}
async function H(n, e, a) {
  let t, i;
  const o = new Promise((r, d) => {
    i = () => d(Error("cancelled"));
  });
  e.addEventListener("abort", i, { once: !0 });
  try {
    if (e.aborted) throw Error("cancelled");
    return await Promise.race([o, (async () => {
      const r = await n.fetchWithAuth(a, { signal: e, cache: "no-store", redirect: "error" });
      if (e.aborted || r.status !== 200 || r.redirected || r.headers.get("content-type")?.split(";")[0].trim() !== "application/json") throw Error("invalid response");
      t = r.body.getReader();
      const d = new TextDecoder("utf-8", { fatal: !0 });
      let g = "", l = 0;
      for (; ; ) {
        const { done: h, value: p } = await t.read();
        if (e.aborted) throw Error("cancelled");
        if (h) break;
        if (l += p.byteLength, l > 524288) throw Error("excessive response");
        g += d.decode(p, { stream: !0 });
      }
      return JSON.parse(g + d.decode());
    })()]);
  } finally {
    e.removeEventListener("abort", i), t && t.cancel().catch(() => {
    });
  }
}
class F extends HTMLElement {
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
    for (const a of this.shadowRoot.querySelectorAll("[data-message]")) a.textContent = b[a.dataset.message];
    const e = this.shadowRoot.querySelector("#menu");
    e.textContent = "☰", e.setAttribute("aria-label", b.menu), e.addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: !0, composed: !0 }))), this.shadowRoot.querySelector("#refresh").addEventListener("click", () => this.#r()), this.#l();
  }
  set hass(e) {
    const a = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e, a && this.#r();
  }
  connectedCallback() {
    this.#r();
  }
  disconnectedCallback() {
    this.#a?.abort(), this.#a = void 0, this.#e?.abort(), this.#e = void 0;
  }
  async #r() {
    if (this.#e?.abort(), this.#e = void 0, this.#a?.abort(), this.#a = void 0, this.#n = void 0, this.#i = this.#t?.user?.is_admin === !0 ? "loading" : "admin", this.#l(), !this.isConnected || this.#i === "admin") return;
    const e = new AbortController();
    this.#a = e;
    const a = setTimeout(() => e.abort(), 15e3);
    try {
      const t = await V(this.#t, e.signal);
      if (e !== this.#a) return;
      this.#n = t, this.#i = t.panels.length ? null : "empty";
    } catch {
      e === this.#a && (this.#i = "failed");
    } finally {
      clearTimeout(a), e === this.#a && (this.#a = void 0, this.#l());
    }
  }
  async #d(e, a) {
    this.#e?.abort(), this.#s && (this.#s.textContent = ""), this.#s = a;
    const t = new AbortController();
    this.#e = t;
    const i = setTimeout(() => t.abort(), 15e3);
    a.textContent = b.checklistLoading;
    try {
      const o = await H(this.#t, t.signal, `/api/panel_assistant/fleet/${encodeURIComponent(e)}/provisioning`);
      if (this.#e !== t) return;
      if (!o || !Array.isArray(o.items) || o.items.length > 32 || typeof o.needs_updated_client != "boolean") throw Error("invalid plan");
      const r = o.items.map((d) => {
        if (!Object.hasOwn(L, d.id) || !Object.hasOwn(_, d.status)) throw Error("invalid item");
        return `${L[d.id]}: ${_[d.status]}`;
      });
      a.textContent = [...r, o.needs_updated_client ? b.checklistNew : "", b.checklistHelp].filter(Boolean).join(`
`);
    } catch {
      this.#e === t && (a.textContent = b.checklistFailed);
    } finally {
      clearTimeout(i), this.#e === t && (this.#e = void 0);
    }
  }
  #l() {
    this.shadowRoot.querySelector("#status").textContent = this.#i ? b[this.#i] : this.#n?.truncated ? b.truncated : "", this.shadowRoot.querySelector("#refresh").disabled = this.#i === "admin" || this.#i === "loading";
    const e = this.shadowRoot.querySelector("#panels");
    e.replaceChildren();
    for (const a of this.#n?.panels ?? []) {
      const t = document.createElement("article"), i = (d, g) => {
        const l = document.createElement(d);
        return l.textContent = g, t.append(l), l;
      };
      i("h2", a.name), i("p", b[a.available ? "available" : "unavailable"]), a.version !== null && i("p", `${b.version}: ${a.version}`), a.status_available ? i("p", `${b.warnings}: ${a.warning_count}`) : a.available && i("p", b.diagnostics), i("a", b.settings).href = `/config/integrations/integration/panel_assistant#config_entry=${encodeURIComponent(a.entry_id)}`;
      const o = i("button", b.checklist);
      o.disabled = !a.available;
      const r = i("p", "");
      r.setAttribute("role", "status"), r.style.whiteSpace = "pre-line", o.addEventListener("click", () => this.#d(a.entry_id, r)), e.append(t);
    }
  }
}
customElements.get("panel-assistant-fleet") || customElements.define("panel-assistant-fleet", F);
const O = "/api/panel_assistant/usb/release", R = 64 * 1024 * 1024, Z = 1800 * 1e3, K = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, X = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, ee = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], x = (n, e) => typeof e == "string" && n.exec(e)?.[0] === e, U = (n, e) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === e.length && e.every((a) => Object.hasOwn(n, a));
class k extends Error {
  constructor(e) {
    super(e), this.name = "HandoffError", this.code = e;
  }
}
function u(n, e = "invalid_response") {
  if (!n) throw new k(e);
}
function z(n, e, a = !1) {
  u(typeof n == "string" && n.length <= Math.ceil(e / 3) * 4 && x(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, n));
  const t = atob(n);
  return u(btoa(t) === n && t.length > 0 && (a ? t.length === e : t.length <= e)), Uint8Array.from(t, (i) => i.charCodeAt(0));
}
async function P(n, e, a, t = null) {
  u(n.status === 200 && !n.redirected && n.body);
  const i = n.headers.get("content-length");
  if (i !== null) {
    u(x(/0|[1-9][0-9]*/, i));
    const l = Number(i);
    u(Number.isSafeInteger(l) && l <= e && (t === null || l === t));
  }
  const o = n.body.getReader(), r = () => {
    o.cancel().catch(() => {
    });
  };
  a.addEventListener("abort", r, { once: !0 });
  const d = [];
  let g = 0;
  try {
    for (; ; ) {
      u(!a.aborted, "cancelled");
      const l = await o.read();
      if (u(!a.aborted, "cancelled"), l.done) break;
      g += l.value.byteLength, u(g <= e && (t === null || g <= t)), d.push(l.value);
    }
    return u(g > 0 && (i === null || g === Number(i)) && (t === null || g === t)), new Blob(d);
  } finally {
    a.removeEventListener("abort", r), r(), o.releaseLock();
  }
}
function te(n, e, {
  rcTag: a = null,
  onState: t = () => {
  },
  windowObject: i = window,
  timeoutMs: o = 3e5
} = {}) {
  let r, d;
  const g = new Promise((s, c) => {
    r = s, d = c;
  }), l = new AbortController();
  let h = !1, p, m, y, A, N = !1, D = !1, v, C, S;
  const j = () => {
    clearInterval(C), clearTimeout(S), v = void 0, i.removeEventListener("message", T);
  }, I = (s) => {
    try {
      t(s);
    } catch {
    }
  }, w = (s = null) => {
    if (!h) {
      if (h = !0, l.abort(), clearTimeout(m), I(s ?? "verified"), s) {
        j(), d(new k(s));
        return;
      }
      C = setInterval(() => {
        p.closed && j();
      }, 2e3), S = setTimeout(j, Z), r();
    }
  };
  async function B() {
    try {
      I("preparing"), u(!h, "cancelled");
      const s = await n.fetchWithAuth(O, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(a === null ? {} : { release_candidate: a }),
        redirect: "error",
        signal: l.signal
      });
      u(!h, "cancelled"), u(s.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const c = JSON.parse(await (await P(s, 8192, l.signal)).text());
      u(!h, "cancelled"), u(U(c, ee) && x(/[0-9a-f]{32}/, c.id) && typeof c.tag == "string" && c.tag.length <= 64 && (a === null ? x(K, c.tag) : c.tag === a) && x(/[0-9a-f]{64}/, c.apk_sha256) && Number.isSafeInteger(c.apk_size) && c.apk_size > 0 && c.apk_size <= R);
      const E = {
        tag: c.tag,
        checksum: z(c.checksum, 512),
        checksumSignature: z(c.checksum_signature, 256, !0),
        descriptor: z(c.descriptor, 4096),
        descriptorSignature: z(c.descriptor_signature, 256, !0)
      };
      I("downloading"), u(!h, "cancelled");
      const G = await n.fetchWithAuth(`${O}/${c.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: l.signal
      });
      u(!h, "cancelled");
      const W = await P(G, R, l.signal, c.apk_size);
      u(!h && !p.closed, "window_closed"), D = !0, v = { type: "ha-paneld/usb-bundle", nonce: A, bundle: E, apk: W }, p.postMessage(v, y), I("verifying");
    } catch (s) {
      w(s instanceof k ? s.code : "delivery_failed");
    }
  }
  function T(s) {
    if (!(s.source !== p || s.origin !== y || !U(s.data, ["type", "nonce"]) || s.data.nonce !== A)) {
      if (s.data.type === "ha-paneld/usb-ready") {
        !N && !h ? (N = !0, B()) : v && !p.closed && p.postMessage(v, y);
        return;
      }
      h || (s.data.type === "ha-paneld/usb-verified" && D ? w() : s.data.type === "ha-paneld/usb-error" && w("verification_failed"));
    }
  }
  try {
    u(n && typeof n.fetchWithAuth == "function" && (a === null || a.length <= 64 && x(X, a)) && Number.isSafeInteger(o) && o > 0 && o <= 3e5, "invalid_request");
    const s = new URL(e);
    u(!s.username && !s.password && !s.hash && (s.protocol === "https:" || s.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(s.hostname)), "invalid_destination"), y = s.origin;
    const c = new Uint8Array(16);
    i.crypto.getRandomValues(c), A = Array.from(c, (E) => E.toString(16).padStart(2, "0")).join(""), s.hash = new URLSearchParams({ ha_origin: i.location.origin, nonce: A, rc: a ?? "" }).toString(), i.addEventListener("message", T), p = i.open(s.href, "_blank"), u(p, "popup_blocked"), m = setTimeout(() => w("timeout"), o), I("waiting");
  } catch (s) {
    w(s instanceof k ? s.code : "invalid_request");
  }
  return { completion: g, cancel: () => {
    w("cancelled"), j();
  } };
}
const ae = 30, Q = 8192, ne = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, ie = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, q = (n, e) => n !== null && typeof n == "object" && !Array.isArray(n) && Object.keys(n).length === e.length && e.every((a) => Object.hasOwn(n, a));
function f(n) {
  if (!n) throw new Error("Invalid release catalogue");
}
function re(n) {
  f(q(n, ["releases"]) && Array.isArray(n.releases) && n.releases.length <= ae);
  const e = /* @__PURE__ */ new Set();
  let a = 0;
  return Object.freeze(n.releases.map((t) => (f(q(t, ["tag", "prerelease"]) && typeof t.prerelease == "boolean" && typeof t.tag == "string" && t.tag.length <= 64 && (t.prerelease ? ie : ne).exec(t.tag)?.[0] === t.tag && !e.has(t.tag)), e.add(t.tag), t.prerelease || f(++a <= 1), Object.freeze({ tag: t.tag, prerelease: t.prerelease }))));
}
async function se(n, { signal: e, timeoutMs: a = 15e3 } = {}) {
  const t = new AbortController(), i = () => t.abort();
  e?.addEventListener("abort", i, { once: !0 }), e?.aborted && i();
  const o = setTimeout(i, a);
  let r, d;
  const g = new Promise((l, h) => {
    d = () => h(new Error("Release catalogue cancelled"));
  });
  t.signal.addEventListener("abort", d, { once: !0 });
  try {
    return f(!t.signal.aborted), await Promise.race([g, (async () => {
      const l = await n.fetchWithAuth("/api/panel_assistant/usb/releases", {
        method: "GET",
        redirect: "error",
        signal: t.signal
      });
      f(!t.signal.aborted && l.status === 200 && !l.redirected && l.body && l.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const h = l.headers.get("content-length");
      f(h === null || /^(0|[1-9][0-9]*)$/.exec(h)?.[0] === h && Number(h) <= Q), r = l.body.getReader();
      const p = [];
      let m = 0;
      for (; ; ) {
        const y = await r.read();
        if (f(!t.signal.aborted), y.done) break;
        m += y.value.byteLength, f(m <= Q), p.push(y.value);
      }
      return f(m > 0 && (h === null || m === Number(h))), re(JSON.parse(await new Blob(p).text()));
    })()]);
  } finally {
    clearTimeout(o), e?.removeEventListener("abort", i), t.signal.removeEventListener("abort", d), t.abort(), r && r.cancel().catch(() => {
    });
  }
}
const oe = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDgiIGhlaWdodD0iMTA4IiB2aWV3Qm94PSIwIDAgMTA4IDEwOCI+CjxwYXRoIGQ9Ik0yOCwzMiBoNTIgYTQsNCAwIDAgMSA0LDQgdjM3IGE0LDQgMCAwIDEgLTQsNCBoLTUyIGE0LDQgMCAwIDEgLTQsLTQgdi0zNyBhNCw0IDAgMCAxIDQsLTQgeiIgZmlsbD0iIzM3NDc0RiIvPgo8cGF0aCBkPSJNMjksMzUgaDUwIGEyLDIgMCAwIDEgMiwyIHYzNSBhMiwyIDAgMCAxIC0yLDIgaC01MCBhMiwyIDAgMCAxIC0yLC0yIHYtMzUgYTIsMiAwIDAgMSAyLC0yIHoiIGZpbGw9IiMwRTE2MjAiLz4KPGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoNDIuMDAsNDIuNTApIHNjYWxlKDAuMTAwMCkiPgo8cGF0aCBmaWxsPSIjRjJGNEY5IiBkPSJNMjQwIDIyNC44MTNDMjQwIDIzMy4wNjMgMjMzLjI1IDIzOS44MTMgMjI1IDIzOS44MTNIMTVDNi43NSAyMzkuODEzIDAgMjMzLjA2MyAwIDIyNC44MTNWMTM0LjgxM0MwIDEyNi41NjMgNC43NyAxMTUuMDQzIDEwLjYxIDEwOS4yMDNMMTA5LjM5IDEwLjQyM0MxMTUuMjIgNC41OTMwNCAxMjQuNzcgNC41OTMwNCAxMzAuNiAxMC40MjNMMjI5LjM5IDEwOS4yMTNDMjM1LjIyIDExNS4wNDMgMjQwIDEyNi41NzMgMjQwIDEzNC44MjNWMjI0LjgyM1YyMjQuODEzWiIvPgo8cGF0aCBmaWxsPSIjMThCQ0YyIiBkPSJNMjI5LjM5IDEwOS4yMDNMMTMwLjYxIDEwLjQyM0MxMjQuNzggNC41OTMwNCAxMTUuMjMgNC41OTMwNCAxMDkuNCAxMC40MjNMMTAuNjEgMTA5LjIwM0M0Ljc4IDExNS4wMzMgMCAxMjYuNTYzIDAgMTM0LjgxM1YyMjQuODEzQzAgMjMzLjA2MyA2Ljc1IDIzOS44MTMgMTUgMjM5LjgxM0gxMDcuMjdMNjYuNjQgMTk5LjE4M0M2NC41NSAxOTkuOTAzIDYyLjMyIDIwMC4zMTMgNjAgMjAwLjMxM0M0OC43IDIwMC4zMTMgMzkuNSAxOTEuMTEzIDM5LjUgMTc5LjgxM0MzOS41IDE2OC41MTMgNDguNyAxNTkuMzEzIDYwIDE1OS4zMTNDNzEuMyAxNTkuMzEzIDgwLjUgMTY4LjUxMyA4MC41IDE3OS44MTNDODAuNSAxODIuMTQzIDgwLjA5IDE4NC4zNzMgNzkuMzcgMTg2LjQ2M0wxMTEgMjE4LjA5M1YxMDIuMjEzQzEwNC4yIDk4Ljg3MyA5OS41IDkxLjg5MyA5OS41IDgzLjgyM0M5OS41IDcyLjUyMyAxMDguNyA2My4zMjMgMTIwIDYzLjMyM0MxMzEuMyA2My4zMjMgMTQwLjUgNzIuNTIzIDE0MC41IDgzLjgyM0MxNDAuNSA5MS44OTMgMTM1LjggOTguODczIDEyOSAxMDIuMjEzVjE4My40ODNMMTYwLjQ2IDE1Mi4wMjNDMTU5Ljg0IDE1MC4wNjMgMTU5LjUgMTQ3Ljk4MyAxNTkuNSAxNDUuODIzQzE1OS41IDEzNC41MjMgMTY4LjcgMTI1LjMyMyAxODAgMTI1LjMyM0MxOTEuMyAxMjUuMzIzIDIwMC41IDEzNC41MjMgMjAwLjUgMTQ1LjgyM0MyMDAuNSAxNTcuMTIzIDE5MS4zIDE2Ni4zMjMgMTgwIDE2Ni4zMjNDMTc3LjUgMTY2LjMyMyAxNzUuMTIgMTY1Ljg1MyAxNzIuOTEgMTY1LjAzM0wxMjkgMjA4Ljk0M1YyMzkuODIzSDIyNUMyMzMuMjUgMjM5LjgyMyAyNDAgMjMzLjA3MyAyNDAgMjI0LjgyM1YxMzQuODIzQzI0MCAxMjYuNTczIDIzNS4yMyAxMTUuMDUzIDIyOS4zOSAxMDkuMjEzVjEwOS4yMDNaIi8+CjwvZz4KPC9zdmc+Cg==", le = Object.freeze(["Version", "Connect", "Install", "Set up"]);
function de(n) {
  return le.map((e, a) => a < n ? `<li class="done">${e}</li>` : a === n ? `<li class="current" aria-current="step">${e}</li>` : `<li>${e}</li>`).join("");
}
const $ = "--bg:#f2f3f5;--card:#fff;--card-head:#e7ebef;--card-border:#d9dde3;--divider:#e4e7ec;--input-bg:#fafbfc;--border:#c4cad2;--border-strong:#b6bec8;--text:#1b2430;--dim:#6a7480;--accent:#1e56a8;--ok:#3f7d49;--bad:#a02c20;--disabled-bg:#e2e5e9;--disabled-fg:#9aa3ad;--shadow:rgba(0,0,0,.18)", Y = "--bg:#111;--card:#181818;--card-head:#222;--card-border:#242424;--divider:#2a2a2a;--input-bg:#161616;--border:#383838;--border-strong:#444;--text:#eee;--dim:#888;--accent:#9af;--ok:#8a8;--bad:#ffb3a6;--disabled-bg:#222;--disabled-fg:#666;--shadow:#000", ce = `
:root,:host{color-scheme:light dark;${$};--primary:#2557a7;--primary-text:#fff;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root,:host{${Y}}}
:host([theme=light]){color-scheme:light;${$}}
:host([theme=dark]){color-scheme:dark;${Y}}
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
class he extends HTMLElement {
  #t;
  #a;
  #e;
  // A finished transfer keeps answering a reloaded installer window until this
  // page goes away or a new transfer starts.
  #s;
  #i = "ready";
  #n;
  #r = "loading";
  #d = [];
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>${ce}
      :host{display:block;min-height:100%;background:var(--bg);padding:24px 16px}
      .card p.status{color:var(--text);margin:14px 0 0}
    </style><main class="wiz">
      <div class="wiz-brand"><img src="${oe}" alt=""><span>ha-paneld</span></div>
      <ol class="wiz-dots" aria-label="Progress">${de(0)}</ol>
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
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#c()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.shadowRoot.querySelector("#retry").addEventListener("click", () => this.#l()), this.shadowRoot.querySelector("#release").addEventListener("change", () => this.#o()), this.#o();
  }
  set hass(e) {
    const a = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e;
    const t = e?.themes?.darkMode;
    typeof t == "boolean" && this.setAttribute?.("theme", t ? "dark" : "light"), a && (this.#e?.cancel(), this.#l()), this.#o();
  }
  set panel(e) {
    const a = this.#a?.config?.installer_url !== e?.config?.installer_url;
    a && this.#e?.cancel(), this.#a = e, a && this.#l(), this.#o();
  }
  connectedCallback() {
    this.#l();
  }
  disconnectedCallback() {
    this.#e?.cancel(), this.#s?.cancel(), this.#s = void 0, this.#n?.abort(), this.#n = void 0;
  }
  async #l() {
    if (this.#n?.abort(), this.#n = void 0, this.#d = [], this.#r = "loading", this.shadowRoot.querySelector("#release").replaceChildren(), this.#o(), !this.isConnected || this.#t?.user?.is_admin !== !0 || !this.#a?.config?.installer_url) return;
    const e = new AbortController();
    this.#n = e;
    try {
      const a = await se(this.#t, { signal: e.signal });
      if (this.#n !== e) return;
      this.#d = a, this.#r = a.length ? "ready" : "empty";
      const t = this.shadowRoot.querySelector("#release"), i = document.createElement("option");
      i.value = "", i.textContent = M.choose, i.disabled = !0, t.append(i);
      const o = a.find((r) => !r.prerelease)?.tag ?? "";
      for (const r of a) {
        const d = document.createElement("option");
        d.value = r.tag;
        const g = r.tag === o ? M.recommended : r.prerelease ? M.testing : "";
        d.textContent = `${r.tag.replace(/^v/, "")}${g ? ` (${g})` : ""}`, t.append(d);
      }
      t.value = o;
    } catch {
      if (this.#n !== e) return;
      this.#r = "catalogError";
    } finally {
      this.#n === e && (this.#n = void 0, this.#o());
    }
  }
  #o() {
    const e = this.#t?.user?.is_admin === !0, a = typeof this.#a?.config?.installer_url == "string" && this.#a.config.installer_url.length > 0, t = this.#d.find((d) => d.tag === this.shadowRoot.querySelector("#release").value);
    this.shadowRoot.querySelector("#start").disabled = !e || !a || !!this.#e || !t, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e || this.#r !== "ready";
    const i = this.shadowRoot.querySelector("#catalog-status");
    i.textContent = e && a && this.#r !== "ready" ? M[this.#r] : "", i.hidden = !i.textContent, this.shadowRoot.querySelector("#retry").hidden = !e || !a || !["catalogError", "empty"].includes(this.#r), this.shadowRoot.querySelector("#cancel").hidden = !this.#e;
    const o = e ? a ? this.#i : "unavailable" : "admin", r = this.shadowRoot.querySelector("#status");
    r.textContent = Object.hasOwn(M, o) ? M[o] : M.failed, r.hidden = !r.textContent;
  }
  #c() {
    if (this.#e || this.#t?.user?.is_admin !== !0) return;
    const e = this.#d.find((t) => t.tag === this.shadowRoot.querySelector("#release").value);
    if (!e || !this.isConnected) return;
    this.#s?.cancel(), this.#s = void 0;
    const a = te(this.#t, this.#a?.config?.installer_url, {
      rcTag: e.prerelease ? e.tag : null,
      onState: (t) => {
        this.#i = t, this.#o();
      }
    });
    this.#e = a, this.#o(), a.completion.then(() => {
      this.#e === a && (this.#s = a);
    }, () => {
    }).finally(() => {
      this.#e === a && (this.#e = void 0), this.#o();
    });
  }
}
customElements.get("panel-assistant-usb-install") || customElements.define("panel-assistant-usb-install", he);
