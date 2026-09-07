const L = "/api/ha_paneld/usb/release";
const z = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/, B = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc[1-9][0-9]*$/, N = [
  "id",
  "tag",
  "checksum",
  "checksum_signature",
  "descriptor",
  "descriptor_signature",
  "apk_size",
  "apk_sha256"
], f = (a, t) => typeof t == "string" && a.exec(t)?.[0] === t, H = (a, t) => a !== null && typeof a == "object" && !Array.isArray(a) && Object.keys(a).length === t.length && t.every((n) => Object.hasOwn(a, n));
class _ extends Error {
  constructor(t) {
    super(t), this.name = "HandoffError", this.code = t;
  }
}
function i(a, t = "invalid_response") {
  if (!a) throw new _(t);
}
function w(a, t, n = !1) {
  i(typeof a == "string" && a.length <= Math.ceil(t / 3) * 4 && f(/(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?/, a));
  const r = atob(a);
  return i(btoa(r) === a && r.length > 0 && (n ? r.length === t : r.length <= t)), Uint8Array.from(r, (o) => o.charCodeAt(0));
}
async function q(a, t, n, r = null) {
  i(a.status === 200 && !a.redirected && a.body);
  const o = a.headers.get("content-length");
  if (o !== null) {
    i(f(/0|[1-9][0-9]*/, o));
    const l = Number(o);
    i(Number.isSafeInteger(l) && l <= t && (r === null || l === r));
  }
  const c = a.body.getReader(), p = () => {
    c.cancel().catch(() => {
    });
  };
  n.addEventListener("abort", p, { once: !0 });
  const g = [];
  let h = 0;
  try {
    for (; ; ) {
      i(!n.aborted, "cancelled");
      const l = await c.read();
      if (i(!n.aborted, "cancelled"), l.done) break;
      h += l.value.byteLength, i(h <= t && (r === null || h <= r)), g.push(l.value);
    }
    return i(h > 0 && (o === null || h === Number(o)) && (r === null || h === r)), new Blob(g);
  } finally {
    n.removeEventListener("abort", p), p(), c.releaseLock();
  }
}
function I(a, t, {
  rcTag: n = null,
  onState: r = () => {
  },
  windowObject: o = window,
  timeoutMs: c = 3e5
} = {}) {
  let p, g;
  const h = new Promise((e, s) => {
    p = e, g = s;
  }), l = new AbortController();
  let d = !1, m, S, k, y, x = !1, R = !1;
  const b = (e) => {
    try {
      r(e);
    } catch {
    }
  }, u = (e = null) => {
    d || (d = !0, l.abort(), clearTimeout(S), o.removeEventListener("message", E), b(e ?? "verified"), e ? g(new _(e)) : p());
  };
  async function C() {
    try {
      b("preparing"), i(!d, "cancelled");
      const e = await a.fetchWithAuth(L, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(n === null ? {} : { release_candidate: n }),
        redirect: "error",
        signal: l.signal
      });
      i(!d, "cancelled"), i(e.headers.get("content-type")?.split(";")[0].trim() === "application/json");
      const s = JSON.parse(await (await q(e, 8192, l.signal)).text());
      i(!d, "cancelled"), i(H(s, N) && f(/[0-9a-f]{32}/, s.id) && typeof s.tag == "string" && s.tag.length <= 64 && (n === null ? f(z, s.tag) : s.tag === n) && f(/[0-9a-f]{64}/, s.apk_sha256) && Number.isSafeInteger(s.apk_size) && s.apk_size > 0 && s.apk_size <= 67108864);
      const v = {
        tag: s.tag,
        checksum: w(s.checksum, 512),
        checksumSignature: w(s.checksum_signature, 256, !0),
        descriptor: w(s.descriptor, 4096),
        descriptorSignature: w(s.descriptor_signature, 256, !0)
      };
      b("downloading"), i(!d, "cancelled");
      const T = await a.fetchWithAuth(`${L}/${s.id}/apk`, {
        method: "GET",
        redirect: "error",
        signal: l.signal
      });
      i(!d, "cancelled");
      const U = await q(T, 67108864, l.signal, s.apk_size);
      i(!d && !m.closed, "window_closed"), R = !0, m.postMessage({ type: "ha-paneld/usb-bundle", nonce: y, bundle: v, apk: U }, k), b("verifying");
    } catch (e) {
      u(e instanceof _ ? e.code : "delivery_failed");
    }
  }
  function E(e) {
    d || e.source !== m || e.origin !== k || !H(e.data, ["type", "nonce"]) || e.data.nonce !== y || (e.data.type === "ha-paneld/usb-ready" && !x ? (x = !0, C()) : e.data.type === "ha-paneld/usb-verified" && R ? u() : e.data.type === "ha-paneld/usb-error" && u("verification_failed"));
  }
  try {
    i(a && typeof a.fetchWithAuth == "function" && (n === null || n.length <= 64 && f(B, n)) && Number.isSafeInteger(c) && c > 0 && c <= 3e5, "invalid_request");
    const e = new URL(t);
    i(!e.username && !e.password && !e.hash && (e.protocol === "https:" || e.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(e.hostname)), "invalid_destination"), k = e.origin;
    const s = new Uint8Array(16);
    o.crypto.getRandomValues(s), y = Array.from(s, (v) => v.toString(16).padStart(2, "0")).join(""), e.hash = new URLSearchParams({ ha_origin: o.location.origin, nonce: y, rc: n ?? "" }).toString(), o.addEventListener("message", E), m = o.open(e.href, "_blank"), i(m, "popup_blocked"), S = setTimeout(() => u("timeout"), c), b("waiting");
  } catch (e) {
    u(e instanceof _ ? e.code : "invalid_request");
  }
  return { completion: h, cancel: () => u("cancelled") };
}
const A = Object.freeze({
  title: "Install a panel using USB",
  introduction: "Connect the panel to this browser’s computer or mobile device, not to the Home Assistant server. USB debugging and Android authorization are required.",
  scope: "This experimental installer supports clean installation only. Existing installations are not overwritten. Setup permissions and connecting the panel to Home Assistant remain separate steps. MQTT is unchanged.",
  release: "Release candidate tag (optional)",
  releaseHelp: "Leave blank for the latest stable release, or enter an exact tag such as v1.2.3-rc1. To resume, use the same release and browser as before.",
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
  invalid_request: "Use an exact release candidate tag such as v1.2.3-rc1, or leave it blank for stable.",
  failed: "Release transfer did not complete. Check the installer window and try again. No installation result is implied."
});
class P extends HTMLElement {
  #n;
  #a;
  #e;
  #s = "ready";
  constructor() {
    super(), this.attachShadow({ mode: "open" }), this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#17232d);padding:24px;box-sizing:border-box}
      main{max-width:680px;margin:auto;font:inherit;line-height:1.5}
      h1{font-size:1.6rem}label{display:block;font-weight:600}
      input{display:block;box-sizing:border-box;width:100%;max-width:26rem;padding:12px;font:inherit}
      button{padding:12px 18px;font:inherit;margin:8px 8px 8px 0;cursor:pointer}
      button:disabled{cursor:default}p{overflow-wrap:anywhere}
      #status{padding:16px;border:1px solid var(--divider-color,#aab6bd);border-radius:8px}
    </style><main>
      <h1 data-message="title"></h1><p data-message="introduction"></p>
      <p data-message="scope"></p>
      <label for="release" data-message="release"></label>
      <input id="release" maxlength="64" autocomplete="off" spellcheck="false" aria-describedby="release-help">
      <p id="release-help" data-message="releaseHelp"></p>
      <button id="start" data-message="start"></button><button id="cancel" data-message="cancel"></button>
      <p id="status" role="status" aria-live="polite"></p>
    </main>`;
    for (const t of this.shadowRoot.querySelectorAll("[data-message]"))
      t.textContent = A[t.dataset.message];
    this.shadowRoot.querySelector("#start").addEventListener("click", () => this.#i()), this.shadowRoot.querySelector("#cancel").addEventListener("click", () => this.#e?.cancel()), this.#t();
  }
  set hass(t) {
    this.#n = t, this.#t();
  }
  set panel(t) {
    this.#a?.config?.installer_url !== t?.config?.installer_url && this.#e?.cancel(), this.#a = t, this.#t();
  }
  disconnectedCallback() {
    this.#e?.cancel();
  }
  #t() {
    const t = this.#n?.user?.is_admin === !0, n = typeof this.#a?.config?.installer_url == "string" && this.#a.config.installer_url.length > 0;
    this.shadowRoot.querySelector("#start").disabled = !t || !n || !!this.#e, this.shadowRoot.querySelector("#cancel").disabled = !this.#e, this.shadowRoot.querySelector("#release").disabled = !!this.#e;
    const r = t ? n ? this.#s : "unavailable" : "admin";
    this.shadowRoot.querySelector("#status").textContent = A[r] ?? A.failed;
  }
  #i() {
    if (this.#e || this.#n?.user?.is_admin !== !0) return;
    const t = this.shadowRoot.querySelector("#release").value.trim(), n = I(this.#n, this.#a?.config?.installer_url, {
      rcTag: t || null,
      onState: (r) => {
        this.#s = r, this.#t();
      }
    });
    this.#e = n, this.#t(), n.completion.catch(() => {
    }).finally(() => {
      this.#e === n && (this.#e = void 0), this.#t();
    });
  }
}
customElements.get("ha-paneld-usb-install") || customElements.define("ha-paneld-usb-install", P);
export {
  A as HA_INSTALL_MESSAGES,
  P as HaPaneldUsbInstallPanel
};
