/* dastor sign-in, for any page of the server. Plain JavaScript, no dependencies.
 *
 * Only a server with users offers it (`sign_in` from GET /v1/session). The key goes to the
 * server once, which answers with a session cookie the page's scripts can't read; the cookie,
 * and so the sign-in, is shared by every page and tab of the server. A sign-in or sign-out in
 * one tab is told to the others, which load themselves again unless they hold unsaved work.
 *
 *   const session = await dastorSession.start({ api: "../v1", headers, button, busy, toast });
 *   session.user, session.sign_in      // who the server takes the page for; whether it offers sign-in
 *   session.open(why)                  // ask for a key (also done on any 401, via session.refused)
 *
 * `required: true` is for the sign-in page: the dialog can't be dismissed, and `signedIn()` is
 * called once the browser is signed in, where other pages load themselves again.
 */
"use strict";

window.dastorSession = (() => {
  const CHANNEL = "dastor.session";
  const state = { user: null, sign_in: false, open, refused, signOut };
  let opt = { api: "../v1", headers: {}, button: null, busy: () => false, required: false, signedIn: () => location.reload() };
  let dialog = null, channel = null;

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
      <p class="hint">Your API key for this server. It is checked and not kept: this browser is signed in to the server's pages until you sign out, or for 12 hours.</p>
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

  // A request the server refused for want of a valid sign-in.
  function refused(why) { if (state.sign_in) open(why); }

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
