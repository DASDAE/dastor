/* dastor sign-in, for any page of the server. Plain JavaScript, no dependencies.
 *
 * Only a server with users offers it (`sign_in` from GET /v1/session). The key goes to the
 * server once, which answers with a session cookie the page's scripts can't read; the cookie,
 * and so the sign-in, is shared by every page and tab of the server. A sign-in or sign-out in
 * one tab is told to the others, which load themselves again unless they hold unsaved work.
 *
 *   const session = await dastorSession.start({ api: "../v1", headers, button, busy, toast });
 *   session.user, session.sign_in      // who the server takes the page for; whether it offers sign-in
 *   session.open(why)                  // ask for a key
 *   session.recheck(why)               // after a 401 or 403: ask again if the sign-in has ended
 *
 * `required: true` is for the sign-in page: the dialog can't be dismissed, and `signedIn()` is
 * called once the browser is signed in, where other pages load themselves again.
 */
"use strict";

window.dastorSession = (() => {
  const CHANNEL = "dastor.session";
  const state = { user: null, sign_in: false, open, recheck, signOut };
  let opt = { api: "../v1", headers: {}, button: null, busy: () => false, required: false, signedIn: () => location.reload() };
  let dialog = null, channel = null, checking = false;

  async function call(method, body) {
    const r = await fetch(`${opt.api}/session`, {
      method, headers: { ...opt.headers, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body && JSON.stringify(body),
    });
    if (r.status === 204) return null;
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`), { status: r.status });
    return j;
  }

  function build() {
    dialog = document.createElement("dialog");
    dialog.id = "signIn"; dialog.setAttribute("aria-labelledby", "signInTitle");
    dialog.innerHTML = `<form id="signInForm">
      <b id="signInTitle">Sign in</b>
      <p class="hint">Your API key for this server. It is checked and not kept: this browser is signed in to the server's pages until you sign out, the sign-in expires, or the server restarts.</p>
      <input id="inpKey" type="password" autocomplete="off" spellcheck="false" placeholder="API key" aria-label="API key" required>
      <div id="signInError" role="alert"></div>
      <div class="row"><button id="btnSignIn" type="submit">Sign in</button><button id="btnSignInCancel" type="button">Cancel</button></div>
    </form>`;
    document.body.append(dialog);
    const key = dialog.querySelector("#inpKey"), error = dialog.querySelector("#signInError"), go = dialog.querySelector("#btnSignIn");
    const cancel = dialog.querySelector("#btnSignInCancel");
    cancel.hidden = opt.required;
    cancel.onclick = () => dialog.close();
    dialog.addEventListener("cancel", (ev) => { if (opt.required) ev.preventDefault(); });  // Esc
    dialog.querySelector("form").onsubmit = async (ev) => {
      ev.preventDefault();
      if (!key.value.trim()) return;
      go.disabled = true;
      try { await call("POST", { key: key.value.trim() }); key.value = ""; changed(opt.signedIn); }
      catch (e) { error.textContent = e.message; key.select(); }
      finally { go.disabled = false; }
    };
  }

  function open(why) {
    if (!state.sign_in || dialog.open) return;
    dialog.querySelector("#signInError").textContent = typeof why === "string" ? why : "";
    dialog.querySelector("#inpKey").value = "";
    dialog.showModal(); dialog.querySelector("#inpKey").focus();
  }

  // The server refused a request. A sign-in may have ended (expired, or the server restarted): ask
  // the server, which also clears an ended one, and ask for the key again if so. A cookie the
  // browser dropped on expiry leaves requests anonymous, refused with 403 rather than 401.
  async function recheck(why) {
    if (!state.sign_in || checking) return;
    checking = true;
    try {
      const was = state.user;
      Object.assign(state, await call("GET")); render();
      if (!state.user) open(was ? "Your sign-in has ended: sign in again." : why);
    } catch (e) { /* unreachable: the request's own error says so */ }
    finally { checking = false; }
  }

  async function signOut() {
    if (opt.busy()) throw new Error("Wait for the edits to be saved, then sign out.");
    await call("DELETE");
    changed(() => location.reload());
  }

  // This tab signed in or out: tell the others, then go on as the new user.
  function changed(then) {
    if (channel) channel.postMessage("changed");  // not to this tab: a channel doesn't hear itself
    then();
  }

  function render() {
    const b = opt.button; if (!b) return;
    b.hidden = !state.sign_in && !state.user;
    b.textContent = state.user || "Sign in";
    b.disabled = !state.sign_in;
    b.title = !state.sign_in ? `Reading the server as ${state.user}`
      : state.user ? `Signed in as ${state.user}: click to sign out` : "Sign in with your API key";
  }

  async function start(options) {
    opt = { ...opt, ...options };
    build();
    try { Object.assign(state, await call("GET")); } catch (e) { /* a server from before sign-in */ }
    render();
    const tell = opt.toast || console.warn;
    if (opt.button) opt.button.onclick = () => (state.user ? signOut().catch((e) => tell(e.message)) : open());
    if ("BroadcastChannel" in window) {
      channel = new BroadcastChannel(CHANNEL);
      channel.onmessage = async () => {
        if (opt.required) { Object.assign(state, await call("GET").catch(() => ({}))); if (state.user) opt.signedIn(); return; }
        if (!opt.busy()) location.reload();
        else tell("You signed in or out in another tab: reload this one once its edits are saved.");
      };
    }
    return state;
  }

  return { start, state };
})();
