/* dastor sign-in, for any page of the server. Plain JavaScript, no dependencies.
 *
 * Only a server with users offers it (`sign_in` from GET /v1/session). The key goes to the
 * server once, which answers with a session cookie the page's scripts can't read; the cookie,
 * and so the sign-in, is shared by every page and tab of the server. A sign-in or sign-out in
 * one tab is told to the others, which load themselves again unless they hold unsaved work.
 *
 *   const session = await dastorSession.start({ api: "../v1", headers, button, busy, toast });
 *   session.user, session.sign_in      // who the server takes the page for; whether it offers sign-in
 *   session.open(why)                  // ask who the user is: a key, or on a trusting server a name
 *   session.recheck(why)               // after a 401 or 403: ask again if the sign-in has ended
 *
 * `required: true` is for the sign-in page: the dialog can't be dismissed, and `signedIn()` is
 * called once the browser is signed in, where other pages load themselves again.
 */
"use strict";

window.dastorSession = (() => {
  const CHANNEL = "dastor.session";
  const state = { user: null, sign_in: false, trusting: false, users: [], open, recheck, signOut };
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

  // The dialog: a key alone, or on a trusting server (`trusting` from GET /v1/session) the users to
  // pick from, a new name, and a key only for a user who has one. Signed in, it also signs out.
  function build() {
    dialog = document.createElement("dialog");
    dialog.id = "signIn"; dialog.setAttribute("aria-labelledby", "signInTitle");
    dialog.innerHTML = `<form id="signInForm">
      <b id="signInTitle">Sign in</b>
      <p class="hint" id="signInHint"></p>
      <div id="signInUsers" role="listbox" aria-label="users"></div>
      <input id="inpUser" type="text" autocomplete="off" spellcheck="false" maxlength="64" placeholder="or a new name" aria-label="user name">
      <input id="inpKey" type="password" autocomplete="off" spellcheck="false" placeholder="API key" aria-label="API key">
      <div id="signInError" role="alert"></div>
      <div class="row"><button id="btnSignIn" type="submit">Sign in</button><button id="btnSignOut" type="button" hidden>Sign out</button><button id="btnSignInCancel" type="button">Cancel</button></div>
    </form>`;
    document.body.append(dialog);
    const $d = (id) => dialog.querySelector("#" + id);
    const key = $d("inpKey"), name = $d("inpUser"), error = $d("signInError"), go = $d("btnSignIn");
    $d("btnSignInCancel").hidden = opt.required;
    $d("btnSignInCancel").onclick = () => dialog.close();
    $d("btnSignOut").onclick = () => signOut().catch((e) => { error.textContent = e.message; });
    dialog.addEventListener("cancel", (ev) => { if (opt.required) ev.preventDefault(); });  // Esc
    $d("signInUsers").onclick = (ev) => {
      const b = ev.target.closest("button[data-user]"); if (!b) return;
      name.value = b.dataset.user; pick();
      if (key.hidden) dialog.querySelector("form").requestSubmit(); else key.focus();
    };
    name.oninput = pick;
    dialog.querySelector("form").onsubmit = async (ev) => {
      ev.preventDefault();
      const who = name.value.trim(), k = key.value.trim();
      if (state.trusting ? !who : !k) return;
      go.disabled = true;
      try {
        await call("POST", state.trusting ? { user: who, ...(k ? { key: k } : {}) } : { key: k });
        key.value = ""; changed(opt.signedIn);
      } catch (e) { error.textContent = e.message; (key.hidden ? name : key).select(); }
      finally { go.disabled = false; }
    };
  }
  // The key field shows only where one is needed: always without trust, else for a user with a key.
  function pick() {
    const name = dialog.querySelector("#inpUser").value.trim();
    const user = (state.users || []).find((u) => u.name === name);
    dialog.querySelector("#inpKey").hidden = state.trusting && !(user && user.key);
    for (const b of dialog.querySelectorAll("#signInUsers button")) b.setAttribute("aria-selected", String(b.dataset.user === name));
  }

  function open(why) {
    if (!state.sign_in || dialog.open) return;
    const $d = (id) => dialog.querySelector("#" + id), esc = (t) => String(t).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    $d("signInError").textContent = typeof why === "string" ? why : "";
    $d("signInTitle").textContent = state.user ? `Signed in as ${state.user}` : "Sign in";
    $d("signInHint").textContent = state.trusting
      ? "Pick who you are, or type a new name. Only a user with a key needs it."
      : "Your API key for this server. It is checked and not kept: this browser is signed in to the server's pages until you sign out, the sign-in expires, or the server restarts.";
    $d("signInUsers").hidden = $d("inpUser").hidden = !state.trusting;
    $d("signInUsers").innerHTML = (state.users || []).map((u) =>
      `<button type="button" data-user="${esc(u.name)}" role="option">${esc(u.name)}${u.key ? ' <span class="hint">key</span>' : ""}</button>`).join("");
    $d("btnSignOut").hidden = !state.user;
    $d("btnSignIn").textContent = state.user ? "Switch" : "Sign in";
    $d("inpUser").value = ""; $d("inpKey").value = "";
    pick();
    dialog.showModal(); (state.trusting ? $d("inpUser") : $d("inpKey")).focus();
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
      : state.user ? `Signed in as ${state.user}: click to switch user or sign out` : "Sign in, to save annotations as yourself";
  }

  async function start(options) {
    opt = { ...opt, ...options };
    build();
    try { Object.assign(state, await call("GET")); } catch (e) { /* a server from before sign-in */ }
    render();
    const tell = opt.toast || console.warn;
    if (opt.button) opt.button.onclick = () => open();
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
