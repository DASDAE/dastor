"""Signing the server's pages in with an API key, kept as a session."""

from urllib.parse import urlencode

import pytest
from conftest import ALICE_KEY, BOB_KEY, auth, live
from fastapi.testclient import TestClient

from dastor.app import create_app
from dastor.auth import ANONYMOUS, SESSION_COOKIE, Principal, Sessions
from dastor.config import Settings
from dastor.proxy import create_proxy

ALICE = Principal("alice", frozenset({"lab"}), True)


def _patches(client, headers=None) -> int:
    response = client.get("/v1/summary", headers=headers)
    assert response.status_code == 200
    return response.json()["patches"]


def _signed_in(client, key=ALICE_KEY):
    response = client.post("/v1/session", json={"key": key})
    assert response.status_code == 200
    return response


class TestSessions:
    def test_a_session_names_its_principal_until_it_ends(self):
        sessions = Sessions()
        one, two = sessions.start(ALICE), sessions.start(ANONYMOUS)
        assert one != two
        assert sessions.get(one) == ALICE
        sessions.end(one)
        assert sessions.get(one) is None
        assert sessions.get(two) == ANONYMOUS
        assert sessions.get("made-up") is None

    def test_a_session_expires(self):
        now = [0.0]
        sessions = Sessions(lifetime=10, clock=lambda: now[0])
        session_id = sessions.start(ALICE)
        now[0] = 9.9
        assert sessions.get(session_id) == ALICE
        now[0] = 10
        assert sessions.get(session_id) is None


class TestSignIn:
    def test_without_users_there_is_no_sign_in(self, viewer):
        assert viewer.get("/v1/session").json() == {"user": None, "sign_in": False}
        assert viewer.post("/v1/session", json={"key": "x"}).status_code == 404
        # a cookie from elsewhere on the host changes nothing
        viewer.cookies.set(SESSION_COOKIE, "anything")
        assert viewer.get("/v1/summary").status_code == 200
        viewer.cookies.clear()

    def test_signing_in_and_out(self, settings):
        with TestClient(create_app(settings)) as client:
            assert client.get("/v1/session").json() == {"user": None, "sign_in": True}
            anonymous = _patches(client)
            cookie = _signed_in(client).headers["set-cookie"]
            # the cookie names a session, not the key
            assert ALICE_KEY not in cookie
            for part in ("HttpOnly", "SameSite=strict", "Path=/;", "Max-Age="):
                assert part.lower() in cookie.lower()
            assert client.get("/v1/session").json()["user"] == "alice"
            assert _patches(client) == _patches(client, auth(ALICE_KEY)) > anonymous
            assert client.delete("/v1/session").status_code == 204
            assert not client.cookies.get(SESSION_COOKIE)
            assert _patches(client) == anonymous

    def test_an_ended_session_is_refused_and_then_cleared(self, settings):
        with TestClient(create_app(settings)) as client:
            _signed_in(client)
            stale = client.cookies.get(SESSION_COOKIE)
            client.delete("/v1/session")
            client.cookies.set(SESSION_COOKIE, stale)
            assert client.get("/v1/summary").status_code == 401
            reply = client.get("/v1/session")
            assert reply.json()["user"] is None
            cleared = reply.headers.get_list("set-cookie")
            assert any(
                x.startswith(f"{SESSION_COOKIE}=") and "max-age=0" in x.lower()
                for x in cleared
            )

    def test_a_session_ends_with_the_server(self, settings):
        with TestClient(create_app(settings)) as client:
            _signed_in(client)
            stale = client.cookies.get(SESSION_COOKIE)
        with TestClient(create_app(settings)) as client:
            client.cookies.set(SESSION_COOKIE, stale)
            assert client.get("/v1/summary").status_code == 401

    def test_a_wrong_key_signs_nobody_in(self, settings):
        with TestClient(create_app(settings)) as client:
            response = client.post("/v1/session", json={"key": "wrong"})
            assert response.status_code == 401
            assert "set-cookie" not in response.headers
            assert client.post("/v1/session", json={"key": ""}).status_code == 422

    def test_the_header_wins_over_the_session(self, settings):
        with TestClient(create_app(settings)) as client:
            _signed_in(client)
            bob = auth(BOB_KEY)
            assert client.get("/v1/session", headers=bob).json()["user"] == "bob"
            assert client.get("/v1/summary", headers=auth("wrong")).status_code == 401
            assert client.get("/v1/session", headers=auth("wrong")).status_code == 401

    def test_another_website_cannot_sign_in(self, settings):
        with TestClient(create_app(settings)) as client:
            elsewhere = {"Origin": "https://evil.example"}
            reply = client.post("/v1/session", json={"key": BOB_KEY}, headers=elsewhere)
            assert reply.status_code == 403
            assert "set-cookie" not in reply.headers

    def test_over_https_the_cookie_is_host_only_and_secure(self, settings):
        app = create_app(settings)
        with TestClient(app, base_url="https://testserver") as client:
            cookie = _signed_in(client).headers["set-cookie"]
            assert cookie.startswith("__Host-dastor_session=")
            assert "secure" in cookie.lower() and "path=/;" in cookie.lower()
            assert client.get("/v1/session").json()["user"] == "alice"


class TestThroughTheProxy:
    def test_the_page_does_not_sign_in_but_reads_as_the_key(self, settings):
        with live(create_app(settings)) as address:
            with TestClient(create_proxy(address, ALICE_KEY)) as alice:
                got = alice.get("/v1/session").json()
                assert got == {"user": "alice", "sign_in": False}
                assert (
                    alice.post("/v1/session", json={"key": BOB_KEY}).status_code == 400
                )
                assert alice.delete("/v1/session").status_code == 400
            with TestClient(create_proxy(address)) as anonymous:
                got = anonymous.get("/v1/session").json()
                assert got == {"user": None, "sign_in": False}


class TestClosedServer:
    """With no rule for visitors, they are turned away before any work."""

    @pytest.fixture
    def closed(self, settings):
        config = settings.model_dump(mode="json")
        config["spool"]["rules"] = [
            {"column": "tag", "pattern": "^public$", "allow": ["user:*"]},
            {"column": "tag", "pattern": ".*", "allow": ["group:lab"]},
        ]
        closed = Settings.model_validate(config)
        assert settings.open_to_visitors and not closed.open_to_visitors
        with TestClient(create_app(closed)) as client:
            yield client

    def test_visitors_do_no_work(self, closed, monkeypatch):
        def no_reading(*args):
            raise AssertionError("a visitor's request read the spool")

        monkeypatch.setattr(closed.app.state.catalog, "view", no_reading)
        for path in ("/v1/summary", "/v1/data", "/v1/viewer/tile", "/v1/annotations"):
            reply = closed.get(path)
            assert reply.status_code == 401
            assert reply.json()["detail"] == "sign in to use this server"
        assert closed.get("/v1/session").json() == {"user": None, "sign_in": True}
        assert closed.get("/health").status_code == 200

    def test_the_viewer_sends_visitors_to_sign_in(self, closed):
        for path in ("/viewer/", "/viewer", "/viewer/index.html"):
            reply = closed.get(path, follow_redirects=False)
            assert reply.status_code == 303
            assert reply.headers["location"] == "/signin?" + urlencode({"next": path})
        # the sign-in page and the files it is made of stay open
        for path in ("/signin", "/viewer/session.js", "/viewer/style.css"):
            assert closed.get(path).status_code == 200
        assert closed.get("/viewer/?a=1", follow_redirects=False).headers[
            "location"
        ] == "/signin?" + urlencode({"next": "/viewer/?a=1"})

    def test_signed_in_users_pass(self, closed):
        assert closed.get("/v1/summary", headers=auth(BOB_KEY)).status_code == 200
        _signed_in(closed, BOB_KEY)
        assert closed.get("/viewer/").status_code == 200
        # `user:*` grants every user: bob sees the public files, alice all
        assert _patches(closed) == 2
        assert _patches(closed, auth(ALICE_KEY)) == 5
        closed.delete("/v1/session")
        assert closed.get("/v1/summary").status_code == 401
