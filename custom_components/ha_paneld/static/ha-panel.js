const L = "/api/ha_paneld/usb/release";
const O = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, P = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, j = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], w = (a, e) => typeof e == "string" && a.exec(e)?.[0] === e, q = (a, e) => a !== null && typeof a == "object" && !Array.isArray(a) && Object.keys(a).length === e.length && e.every((n) => Object.hasOwn(a, n));
class k extends Error {
  constructor(e) {
    super(e), this.name = "HandoffError", this.code = e;
  }
}
function l(a, e = "invalid_response") {
  if (!a) throw new k(e);
}
function S(a, e, n = !1) {
  l(typeof a == "string" && a.length <= Math.ceil(e / 3) * 4 && w(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, a));
  const t = atob(a);
  return l(btoa(t) === a && t.length > 0 && (n ? t.length === e : t.length <= e)), Uint8Array.from(t, (r) => r.charCodeAt(0));
}
async function T(a, e, n, t = null) {
  l(a.status === 200 && !a.redirected && a.body);
  const r = a.headers.get("content-length");
  if (r !== null) {
    l(w(/0|[1-9][0-9]*/, r));
    const o = Number(r);
    l(Number.isSafeInteger(o) && o <= e && (t === null || o === t));
  }
  const c = a.body.getReader(), h = () => {
    c.cancel().catch(() => {
    });
  };
  n.addEventListener("abort", h, { once: !0 });
  const p = [];
  let u = 0;
  try {
    for (; ; ) {
      l(!n.aborted, "cancelled");
      const o = await c.read();
      if (l(!n.aborted, "cancelled"), o.done) break;
      u += o.value.byteLength, l(u <= e && (t === null || u <= t)), p.push(o.value);
    }
    return l(u > 0 && (r === null || u === Number(r)) && (t === null || u === t)), new Blob(p);
  } finally {
    n.removeEventListener("abort", h), h(), c.releaseLock();
  }
}
function I(a, e, {
  rcTag: n = null,
  onState: t = () => {
  },
  windowObject: r = window,
  timeoutMs: c = 3e5
} = {}) {
  let h, p;
  const u = new Promise((s, i) => {
    h = s, p = i;
  }), o = new AbortController();
  let d = !1, f, b, y, _, E = !1, C = !1;
  const v = (s) => {
    try {
      t(s);
    } catch {
    }
  }, m = (s = null) => {
    d || (d = !0, o.abort(), clearTimeout(b), r.removeEventListener("message", x), v(s ?? "verified"), s ? p(new k(s)) : h());
  };
  async function z() {
    try {
      v("preparing"), l(!d, "cancelled");
      const s = await a.fetchWithAuth(L, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(n === null ? {} : { release_candidate: n }),
        redirect: "error",
        signal: o.signal
      });
      l(!d, "cancelled"), l(s.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const i = JSON.parse(await (await T(s, 8192, o.signal)).text());
      l(!d, "cancelled"), l(q(i, j) && w(/[0-9a-f]{32}/, i.id) && typeof i.tag == "string" && i.tag.length <= 64 && (n === null ? w(O, i.tag) : i.tag === n) && w(/[0-9a-f]{64}/, i.apk_sha256) && Number.isSafeInteger(i.apk_size) && i.apk_size > 0 && i.apk_size <= 67108864);
      const R = {
        tag: i.tag,
        checksum: S(i.checksum, 512),
        checksumSignature: S(i.checksum_signature, 256, !0),
        descriptor: S(i.descriptor, 4096),
        descriptorSignature: S(i.descriptor_signature, 256, !0)
      };
      v("downloading"), l(!d, "cancelled");
      const B = await a.fetchWithAuth(`${L}/${i.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: o.signal
      });
      l(!d, "cancelled");
      const $ = await T(B, 67108864, o.signal, i.apk_size);
      l(!d && !f.closed, "window_closed"), C = !0, f.postMessage({ type: "ha-paneld/usb-bundle", nonce: _, bundle: R, apk: $ }, y), v("verifying");
    } catch (s) {
      m(s instanceof k ? s.code : "delivery_failed");
    }
  }
  function x(s) {
    d || s.source !== f || s.origin !== y || !q(s.data, ["type", "nonce"]) || s.data.nonce !== _ || (s.data.type === "ha-paneld/usb-ready" && !E ? (E = !0, z()) : s.data.type === "ha-paneld/usb-verified" && C ? m() : s.data.type === "ha-paneld/usb-error" && m("verification_failed"));
  }
  try {
    l(a && typeof a.fetchWithAuth == "function" && (n === null || n.length <= 64 && w(P, n)) && Number.isSafeInteger(c) && c > 0 && c <= 3e5, "invalid_request");
    const s = new URL(e);
    l(!s.username && !s.password && !s.hash && (s.protocol === "https:" || s.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(s.hostname)), "invalid_destination"), y = s.origin;
    const i = new Uint8Array(16);
    r.crypto.getRandomValues(i), _ = Array.from(i, (R) => R.toString(16).padStart(2, "0")).join(""), s.hash = new URLSearchParams({ ha_origin: r.location.origin, nonce: _, rc: n ?? "" }).toString(), r.addEventListener("message", x), f = r.open(s.href, "_blank"), l(f, "popup_blocked"), b = setTimeout(() => m("timeout"), c), v("waiting");
  } catch (s) {
    m(s instanceof k ? s.code : "invalid_request");
  }
  return { completion: u, cancel: () => m("cancelled") };
}
const U = 30, H = 8192, M = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, W = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, N = (a, e) => a !== null && typeof a == "object" && !Array.isArray(a) && Object.keys(a).length === e.length && e.every((n) => Object.hasOwn(a, n));
function g(a) {
  if (!a) throw new Error("Invalid release catalogue");
}
function X(a) {
  g(N(a, ["releases"]) && Array.isArray(a.releases) && a.releases.length <= U);
  const e = /* @__PURE__ */ new Set();
  let n = 0;
  return Object.freeze(a.releases.map((t) => (g(N(t, ["tag", "prerelease"]) && typeof t.prerelease == "boolean" && typeof t.tag == "string" && t.tag.length <= 64 && (t.prerelease ? W : M).exec(t.tag)?.[0] === t.tag && !e.has(t.tag)), e.add(t.tag), t.prerelease || g(++n <= 1), Object.freeze({ tag: t.tag, prerelease: t.prerelease }))));
}
async function G(a, { signal: e, timeoutMs: n = 15e3 } = {}) {
  const t = new AbortController(), r = () => t.abort();
  e?.addEventListener("abort", r, { once: !0 }), e?.aborted && r();
  const c = setTimeout(r, n);
  let h, p;
  const u = new Promise((o, d) => {
    p = () => d(new Error("Release catalogue cancelled"));
  });
  t.signal.addEventListener("abort", p, { once: !0 });
  try {
    return g(!t.signal.aborted), await Promise.race([u, (async () => {
      const o = await a.fetchWithAuth("/api/ha_paneld/usb/releases", {
        method: "GET",
        redirect: "error",
        signal: t.signal
      });
      g(!t.signal.aborted && o.status === 200 && !o.redirected && o.body && o.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const d = o.headers.get("content-length");
      g(d === null || /^(0|[1-9][0-9]*)$/.exec(d)?.[0] === d && Number(d) <= H), h = o.body.getReader();
      const f = [];
      let b = 0;
      for (; ; ) {
        const y = await h.read();
        if (g(!t.signal.aborted), y.done) break;
        b += y.value.byteLength, g(b <= H), f.push(y.value);
      }
      return g(b > 0 && (d === null || b === Number(d))), X(JSON.parse(await new Blob(f).text()));
    })()]);
  } finally {
    clearTimeout(c), e?.removeEventListener("abort", r), t.signal.removeEventListener("abort", p), t.abort(), h && h.cancel().catch(() => {
    });
  }
}
const A = Object.freeze({
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
class J extends HTMLElement {
  #t;
  #r;
  #e;
  #l = "ready";
  #a;
  #s = "loading";
  #i = [];
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
      e.textContent = A[e.dataset.message];
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#c()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.shadowRoot.querySelector("#retry").addEventListener("click", () => this.#o()), this.shadowRoot.querySelector("#release").addEventListener("change", () => this.#n()), this.#n();
  }
  set hass(e) {
    const n = this.#t?.user?.id !== e?.user?.id || this.#t?.user?.is_admin !== e?.user?.is_admin || this.#t?.connection !== e?.connection || this.#t?.auth !== e?.auth;
    this.#t = e, n && (this.#e?.cancel(), this.#o()), this.#n();
  }
  set panel(e) {
    const n = this.#r?.config?.installer_url !== e?.config?.installer_url;
    n && this.#e?.cancel(), this.#r = e, n && this.#o(), this.#n();
  }
  connectedCallback() {
    this.#o();
  }
  disconnectedCallback() {
    this.#e?.cancel(), this.#a?.abort(), this.#a = void 0;
  }
  async #o() {
    if (this.#a?.abort(), this.#a = void 0, this.#i = [], this.#s = "loading", this.shadowRoot.querySelector("#release").replaceChildren(), this.#n(), !this.isConnected || this.#t?.user?.is_admin !== !0 || !this.#r?.config?.installer_url) return;
    const e = new AbortController();
    this.#a = e;
    try {
      const n = await G(this.#t, { signal: e.signal });
      if (this.#a !== e) return;
      this.#i = n, this.#s = n.length ? "ready" : "empty";
      const t = this.shadowRoot.querySelector("#release"), r = document.createElement("option");
      r.value = "", r.textContent = A.choose, r.disabled = !0, t.append(r);
      for (const c of n) {
        const h = document.createElement("option");
        h.value = c.tag, h.textContent = `${c.tag} — ${c.prerelease ? "Release candidate (testing)" : "Stable"}`, t.append(h);
      }
      t.value = n.find((c) => !c.prerelease)?.tag ?? "";
    } catch {
      if (this.#a !== e) return;
      this.#s = "catalogError";
    } finally {
      this.#a === e && (this.#a = void 0, this.#n());
    }
  }
  #n() {
    const e = this.#t?.user?.is_admin === !0, n = typeof this.#r?.config?.installer_url == "string" && this.#r.config.installer_url.length > 0, t = this.#i.find((c) => c.tag === this.shadowRoot.querySelector("#release").value);
    this.shadowRoot.querySelector("#start").disabled = !e || !n || !!this.#e || !t, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e || this.#s !== "ready", this.shadowRoot.querySelector("#catalog-status").textContent = e && n && this.#s !== "ready" ? A[this.#s] : "", this.shadowRoot.querySelector("#retry").hidden = !e || !n || !["catalogError", "empty"].includes(this.#s);
    const r = e ? n ? this.#l : "unavailable" : "admin";
    this.shadowRoot.querySelector("#status").textContent = A[r] ?? A.failed;
  }
  #c() {
    if (this.#e || this.#t?.user?.is_admin !== !0) return;
    const e = this.#i.find((t) => t.tag === this.shadowRoot.querySelector("#release").value);
    if (!e || !this.isConnected) return;
    const n = I(this.#t, this.#r?.config?.installer_url, {
      rcTag: e.prerelease ? e.tag : null,
      onState: (t) => {
        this.#l = t, this.#n();
      }
    });
    this.#e = n, this.#n(), n.completion.catch(() => {
    }).finally(() => {
      this.#e === n && (this.#e = void 0), this.#n();
    });
  }
}
customElements.get("ha-paneld-usb-install") || customElements.define("ha-paneld-usb-install", J);
export {
  A as HA_INSTALL_MESSAGES,
  J as HaPaneldUsbInstallPanel
};
