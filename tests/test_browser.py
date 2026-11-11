"""
The viewer, driven in a headless Chromium over the DevTools protocol.

Needs Chromium or Chrome on the path. Deselected by default; run with
`uv run pytest -m browser`.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import dascore as dc
import pytest
import uvicorn
from conftest import VIEW_KEY

from dastor.annotations import AnnotationStore
from dastor.app import create_app
from dastor.config import Limits

websocket = pytest.importorskip("websocket")

pytestmark = pytest.mark.browser

_BROWSER = shutil.which("chromium") or shutil.which("google-chrome")


def _free_port() -> int:
    """Return a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Browser:
    """One headless page, with the mouse, the keyboard and a JS console."""

    def __init__(self, url: str):
        # A snap's Chromium can only write a profile under its own folder.
        snap = "snap" in os.path.realpath(_BROWSER)
        base = Path.home() / "snap/chromium/common" if snap else None
        self.profile = tempfile.mkdtemp(prefix="dastor-test-", dir=base)
        port = _free_port()
        command = [
            _BROWSER,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--disable-gpu",
            "--window-size=1400,900",
            "--remote-allow-origins=*",
            "about:blank",
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.errors: list[str] = []
        self._count = 0
        self.socket = None
        try:
            page = self._page(port)
            self.socket = websocket.create_connection(
                page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True
            )
            for method in ("Page.enable", "Runtime.enable", "Log.enable"):
                self.call(method)
            self.call("Page.navigate", url=url)
        except BaseException:
            self.close()  # a browser which did not come up is not left running
            raise

    @staticmethod
    def _page(port: int) -> dict:
        """Return the browser's page target once it is listening."""
        for _ in range(150):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as reply:
                    return next(x for x in json.load(reply) if x["type"] == "page")
            except OSError, StopIteration:
                time.sleep(0.2)
        raise TimeoutError("the browser did not start")

    def call(self, method: str, **params) -> dict:
        """Send one protocol command and return its result."""
        self._count += 1
        number = self._count
        self.socket.send(json.dumps({"id": number, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") == number:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})
            self._note(message)

    def _note(self, message: dict) -> None:
        """Keep the errors the page reports."""
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            text = details.get("exception", {}).get("description")
            self.errors.append(text or details.get("text"))
        elif method == "Runtime.consoleAPICalled" and params["type"] == "error":
            self.errors.append(str(params["args"]))
        elif method == "Log.entryAdded" and params["entry"]["level"] == "error":
            entry = params["entry"]
            self.errors.append(f"{entry['text']} {entry.get('url', '')}")

    def js(self, expression: str):
        """Return the value of a JavaScript expression (awaited)."""
        out = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if "exceptionDetails" in out:
            raise RuntimeError(out["exceptionDetails"])
        return out["result"].get("value")

    def wait(self, expression: str, timeout: float = 30) -> None:
        """Wait until a JavaScript expression is true."""
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expression):
                return
            time.sleep(0.05)
        raise TimeoutError(expression)

    def settle(self) -> None:
        """Wait until every tile is answered, edits are saved and a frame drawn."""
        frame = (
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
        )
        self.wait("window.dastorReady === true")
        self.js(frame)
        self.wait("window.dastorIdle()")
        self.js(frame)

    def _mouse(self, kind: str, x: float, y: float, **more) -> None:
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, **more)

    def click(self, x: float, y: float) -> None:
        """Click the left button at a point of the page."""
        self._mouse("mouseMoved", x, y)
        self._mouse("mousePressed", x, y, button="left", clickCount=1, buttons=1)
        self._mouse("mouseReleased", x, y, button="left", clickCount=1)

    def double_click(self, x: float, y: float) -> None:
        """Click the left button twice at a point of the page."""
        self.click(x, y)
        self._mouse("mousePressed", x, y, button="left", clickCount=2, buttons=1)
        self._mouse("mouseReleased", x, y, button="left", clickCount=2)

    def drag(self, start: tuple, end: tuple, button: str = "left") -> None:
        """Drag with a button (left or middle) from one point to another."""
        (x0, y0), (x1, y1) = start, end
        held = {"button": button, "buttons": {"left": 1, "middle": 4}[button]}
        self._mouse("mouseMoved", x0, y0)
        self._mouse("mousePressed", x0, y0, clickCount=1, **held)
        for step in range(1, 7):
            x, y = x0 + (x1 - x0) * step / 6, y0 + (y1 - y0) * step / 6
            self._mouse("mouseMoved", x, y, **held)
        self._mouse("mouseReleased", x1, y1, button=button, clickCount=1)

    def type(self, text: str) -> None:
        """Type text into whatever has the focus."""
        self.call("Input.insertText", text=text)

    def right_click(self, x: float, y: float) -> None:
        """Click the right button at a point of the page."""
        self._mouse("mouseMoved", x, y)
        self._mouse("mousePressed", x, y, button="right", clickCount=1, buttons=2)
        self._mouse("mouseReleased", x, y, button="right", clickCount=1)

    def wheel(self, x: float, y: float, delta: float) -> None:
        """Turn the wheel at a point of the page."""
        self._mouse("mouseWheel", x, y, deltaX=0, deltaY=delta)

    def key(self, key: str, code: int, modifiers: int = 0) -> None:
        """Press and release a key (modifier 2 is Ctrl)."""
        for kind in ("rawKeyDown", "keyUp"):
            self.call(
                "Input.dispatchKeyEvent",
                type=kind,
                key=key,
                windowsVirtualKeyCode=code,
                modifiers=modifiers,
            )

    def screenshot(self, path: Path) -> None:
        """Save a picture of the page."""
        data = self.call("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        """Close the page and remove its profile."""
        try:
            if self.socket is not None:
                self.socket.close()
        finally:
            self.process.terminate()
            self.process.wait(timeout=10)
            # Chromium's helpers can outlive it for a moment and rewrite files.
            for _ in range(20):
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.25)


@pytest.fixture
def served(viewer_settings):
    """The address of a real server on the viewer's directory."""
    port = _free_port()
    config = uvicorn.Config(create_app(viewer_settings), port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 60
    while not server.started:
        assert thread.is_alive() and time.time() < end, "the server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page(served, viewer_settings):
    """The viewer open in a browser, with another user's set to look at."""
    if not _BROWSER:
        pytest.skip("no Chromium or Chrome on the path")
    row = {"feature_id": "auto_1", "time": "2024-05-01T00:00:05", "distance": 30.0}
    document = {
        "dims": ["distance", "time"],
        "acquisition_key": VIEW_KEY,
        "base_revision": 0,
        "annotations": [row],
        "features": [{"id": "auto_1", "label": "detected"}],
    }
    store = AnnotationStore(viewer_settings.annotations.path)
    store.save("detector", "auto", document, "detector")
    # thirty picks within a second and a few metres of each other
    rows = [
        {
            "feature_id": f"pick_{number}",
            "time": f"2024-05-01T00:03:30.{number:02d}",
            "distance": 100.0 + number / 10,
        }
        for number in range(30)
    ]
    crowd = {**document, "annotations": rows, "features": []}
    store.save("detector", "many", crowd, "detector")
    browser = Browser(f"{served}/viewer/")
    try:
        browser.settle()
        yield browser
        browser.js("1")  # reads any errors the page reported since the last call
    finally:
        browser.close()
    assert not browser.errors


def _pixel(page, time: str, distance: float) -> tuple[float, float]:
    """Return where a time and distance are drawn, in page pixels."""
    point = f"[x2px(parseTime('{time}')), y2px({distance})]"
    box = "cv.getBoundingClientRect()"
    x, y = page.js(
        f"(() => {{ const p = {point}, r = {box};"
        " return [p[0] + r.left, p[1] + r.top]; })()"
    )
    return x, y


def _saved(viewer_settings, name: str = "picks") -> dc.AnnotationSet:
    """Return the set the page saved, read from disk by DASCore."""
    return dc.annotations(viewer_settings.annotations.path / "me" / name)


def _new_set(page, name: str = "picks") -> None:
    """Make an annotation set through the page."""
    page.js(f"newSet('{name}')")
    page.settle()


class TestViewing:
    """The page shows the recording and moves through its levels."""

    def test_opens_on_the_coarse_level(self, page):
        """The whole recording is drawn from the coarse product, gap squeezed."""
        assert page.js("S.levels.map(l => levelName(l.p))") == ["max_100ms", "raw"]
        assert page.js("S.segs.length") == 2
        assert page.js("levelName(activeLevel().p)") == "max_100ms"
        states = page.js("[...tiles.values()].map(t => t.st)")
        assert states and set(states) == {"ok"}

    def test_zooming_in_reaches_the_raw_level(self, page):
        """The wheel zooms to the finest product, whose tiles then load."""
        x, y = _pixel(page, "2024-05-01T00:00:10", 60)
        for _ in range(12):
            page.wheel(x, y, -200)
        page.settle()
        assert page.js("levelName(activeLevel().p)") == "raw"
        raw = "[...tiles.values()].filter(t => t.pid === activeLevel().p.id)"
        assert page.js(f"{raw}.some(t => t.st === 'ok')")
        value = page.js("valueAt(parseTime('2024-05-01T00:00:10'), 60)")
        assert isinstance(value, float)

    def test_arrow_keys_pan(self, page):
        """The arrows move the view a tenth of its size; up shows more distance."""
        before = page.js("[S.view.x0, S.view.y0, S.view.spp, S.view.ypp]")
        page.key("ArrowUp", 38)
        page.key("ArrowRight", 39)
        after = page.js("[S.view.x0, S.view.y0, S.view.spp, S.view.ypp]")
        size = page.js("[plotRect().w, plotRect().h]")
        assert after[0] == pytest.approx(before[0] + size[0] * before[2] / 10)
        assert after[1] == pytest.approx(before[1] + size[1] * before[3] / 10)
        page.key("ArrowDown", 40)
        page.key("ArrowLeft", 37)
        assert page.js("[S.view.x0, S.view.y0]") == pytest.approx(before[:2])

    def test_side_panel_folds_and_help_opens(self, page):
        """The side panel gives its width to the plot; "?" opens the keys."""
        wide = page.js("plotRect().w")
        page.js("document.getElementById('sideHandle').click()")
        assert page.js("document.getElementById('side').hidden") is True
        assert page.js("plotRect().w") == pytest.approx(wide + 340, abs=20)
        page.js("document.getElementById('sideHandle').click()")
        assert page.js("plotRect().w") == pytest.approx(wide)
        assert page.js("document.getElementById('help').open") is False
        page.js("document.getElementById('btnHelp').click()")
        assert page.js("document.getElementById('help').open") is True
        assert "Space and drag" in page.js(
            "document.getElementById('help').textContent"
        )
        page.js("document.getElementById('btnHelpClose').click()")
        assert page.js("document.getElementById('help').open") is False

    def test_middle_button_drags_the_view(self, page):
        """The left button pans with the pan tool only; the middle one always."""
        start = _pixel(page, "2024-05-01T00:00:20", 60)
        end = (start[0] - 120, start[1] + 50)
        before = page.js("[S.view.x0, S.view.y0, S.view.spp, S.view.ypp]")
        assert page.js("S.mode") == "pan"  # the tool to begin with
        page.drag(start, end)
        after = page.js("[S.view.x0, S.view.y0]")
        assert after[0] == pytest.approx(before[0] + 120 * before[2])
        assert after[1] == pytest.approx(before[1] + 50 * before[3])
        page.drag(end, start)
        page.double_click(*start)  # which makes it the select tool
        assert page.js("S.mode") == "select"
        page.drag(start, end)  # a window, on nothing: the view stays
        assert page.js("[S.view.x0, S.view.y0]") == pytest.approx(before[:2])
        page.key("Escape", 27)
        assert page.js("S.mode") == "pan"
        for tool in ("select", "path"):
            page.js(f"setMode('{tool}')" if tool == "select" else "S.mode = 'path'")
            page.drag(start, end, button="middle")
            after = page.js("[S.view.x0, S.view.y0]")
            assert after[0] == pytest.approx(before[0] + 120 * before[2])
            assert after[1] == pytest.approx(before[1] + 50 * before[3])
            page.drag(end, start, button="middle")
        assert page.js("S.draft") is None  # panning placed no point

    def test_tab_pages_through_time(self, page):
        """Tab moves on by a screen less a small overlap; Shift+Tab goes back."""
        x, y = _pixel(page, "2024-05-01T00:00:10", 60)
        for _ in range(
            6
        ):  # a few screens of the recording, so there is somewhere to page to
            page.wheel(x, y, -200)
        page.settle()
        page.js(
            "document.getElementById('btnAuto').focus()"
        )  # Tab pages wherever the focus is
        before, spp = page.js("[S.view.x0, S.view.spp]")
        width = page.js("plotRect().w")
        page.key("Tab", 9)
        assert page.js("S.view.x0") == pytest.approx(before + width * spp * 0.97)
        page.key("Tab", 9, modifiers=8)
        assert page.js("S.view.x0") == pytest.approx(before)
        for _ in range(40):  # paging stops at the end of the recording, and says so
            page.key("Tab", 9)
        end = page.js("tau(S.ext.x0) + displaySpan()")
        assert page.js("S.view.x0") < end < page.js("viewEnd().x1")
        assert "end of the recording" in page.js(
            "document.getElementById('toast').textContent"
        )

    def test_level_bar_holds_a_level_and_back_returns(self, page):
        """The status bar's control holds the view on a level; `b` goes back."""
        names = page.js(
            "[...document.getElementById('levelBar').children].map(b => b.textContent)"
        )
        assert names == ["auto", "max_100ms", "raw"]
        page.js("document.getElementById('levelBar').children[2].click()")
        page.settle()
        assert page.js("levelName(activeLevel().p)") == "raw"
        assert (
            page.js("document.querySelector('#levelBar .pinned').textContent") == "raw"
        )
        page.js("document.getElementById('levelBar').children[0].click()")
        page.settle()
        assert page.js("S.pin") is None
        assert page.js("levelName(activeLevel().p)") == "max_100ms"

        whole = page.js("[S.view.x0, S.view.spp]")
        x, y = _pixel(page, "2024-05-01T00:00:10", 60)
        for _ in range(5):
            page.wheel(x, y, -200)
        page.key("w", 87)  # fit: a jump, so the zoomed view is kept to come back to
        assert page.js("[S.view.x0, S.view.spp]") == pytest.approx(whole)
        page.key("b", 66)
        assert page.js("S.view.spp") < whole[1] / 2

    def test_overview_strip_moves_the_window(self, page):
        """A press on the strip under the axis centres the window there."""
        x, y = _pixel(page, "2024-05-01T00:00:10", 60)
        for _ in range(8):
            page.wheel(x, y, -200)
        page.settle()
        left, top, width = page.js(
            "(() => { const r = cv.getBoundingClientRect(), s = stripRect(plotRect());"
            " return [r.left + s.x, r.top + s.y + s.h / 2, s.w]; })()"
        )
        page.click(left + width * 0.75, top)
        middle = page.js("S.view.x0 + plotRect().w * S.view.spp / 2")
        wanted = page.js("tau(S.ext.x0) + 0.75 * displaySpan()")
        assert middle == pytest.approx(wanted, rel=1e-3)

    def test_colour_bar_sets_the_limits(self, page):
        """Limits typed by hand are held; a click on the colour bar frees them."""
        page.js(
            "(() => { const e = document.getElementById('inpVmax'); e.value = '123';"
            " e.dispatchEvent(new Event('change')); })()"
        )
        assert page.js("[S.disp.vmax, S.manual]") == [123, True]
        left, top = page.js(
            "(() => { const r = cv.getBoundingClientRect(), b = barRect(plotRect());"
            " return [r.left + b.x + b.w / 2, r.top + b.y + b.h / 2]; })()"
        )
        page.click(left, top)
        assert page.js("S.manual") is False
        assert page.js("S.disp.vmax") < 50
        # a diverging map puts zero in the middle, where it is white
        page.js(
            "(() => { const e = document.getElementById('selCmap');"
            " e.value = 'seismic'; e.dispatchEvent(new Event('change')); })()"
        )
        low, high = page.js("[S.disp.vmin, S.disp.vmax]")
        assert low == -high and high > 0
        # and the page has a light theme, which the canvas follows
        page.js("setTheme('dark')")
        page.js("document.getElementById('btnTheme').click()")
        assert page.js("document.documentElement.dataset.theme") == "light"
        assert page.js("C === CHROME.light") is True
        page.js("document.getElementById('btnTheme').click()")
        assert page.js("C === CHROME.dark") is True
        # dragged, the bar moves its limits: the top end the high one, the middle both
        low, high = page.js("[S.disp.vmin, S.disp.vmax]")
        height = page.js("barRect(plotRect()).h")
        page.drag((left, top - 0.45 * height), (left, top - 0.2 * height))
        assert page.js("S.manual") is True
        raised = page.js("S.disp.vmax")
        assert raised > high and page.js("S.disp.vmin") == low
        shown = page.js("+document.getElementById('inpVmax').value")
        assert shown == pytest.approx(raised, rel=1e-5)
        page.drag((left, top), (left, top - 0.1 * height))
        shifted = page.js("[S.disp.vmin, S.disp.vmax]")
        assert shifted[0] < low and shifted[1] < raised
        assert shifted[1] - shifted[0] == pytest.approx(raised - low, rel=1e-3)


class TestAnnotating:
    """Shapes drawn on the page are saved as a DASCore annotation set."""

    def test_draw_edit_and_undo(self, page, viewer_settings):
        """A path and a box are saved; a label, a moved vertex and undo follow."""
        _new_set(page)
        page.js("setMode('path')")
        for when, distance in (("00:00:02", 20), ("00:00:08", 60), ("00:00:14", 100)):
            page.click(*_pixel(page, f"2024-05-01T{when}", distance))
        page.key("Enter", 13)
        page.settle()
        saved = _saved(viewer_settings)
        assert saved.features["geometry"].tolist() == ["path"]
        assert saved.annotations["distance"].round().tolist() == [20, 60, 100]
        assert saved.features["modified_by"].tolist() == ["me"]

        page.js("setMode('box')")
        start = _pixel(page, "2024-05-01T00:03:02", 20)
        page.drag(start, _pixel(page, "2024-05-01T00:03:10", 80))
        page.settle()
        saved = _saved(viewer_settings)
        assert len(saved.features) == 2
        box = saved.annotations.dropna(subset=["time_min"]).iloc[0]
        assert str(box["time_min"])[11:19] in ("00:03:01", "00:03:02")
        assert str(box["time_max"])[11:19] in ("00:03:09", "00:03:10")
        assert 18 <= box["distance_min"] <= 22 and 78 <= box["distance_max"] <= 82

        page.key("z", 90, modifiers=2)
        page.settle()
        assert len(_saved(viewer_settings).features) == 1

        page.js("setMode('select')")
        page.click(*_pixel(page, "2024-05-01T00:00:08", 60))
        label = "document.querySelector('#selected input[data-col=label]')"
        page.js(
            f"(() => {{ const e = {label}; e.value = 'car';"
            " e.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        page.drag(
            _pixel(page, "2024-05-01T00:00:14", 100),
            _pixel(page, "2024-05-01T00:00:14", 120),
        )
        page.settle()
        saved = _saved(viewer_settings)
        assert saved.features["label"].tolist() == ["car"]
        assert saved.annotations["distance"].round().tolist() == [20, 60, 120]
        # five edits; ones made within the save delay share a revision
        assert 3 <= page.js("activeSet().revision") <= 5

    def test_right_click_ends_a_polygon_and_a_drag_moves_it(
        self, page, viewer_settings
    ):
        """A polygon ends with a right-click; the select tool then drags it."""
        _new_set(page)
        page.js("setMode('polygon')")
        corners = (("00:00:10", 20), ("00:00:20", 20), ("00:00:20", 60))
        for when, distance in corners:
            page.click(*_pixel(page, f"2024-05-01T{when}", distance))
        assert page.js("S.draft.pts.length") == 3  # still open: nothing saved yet
        page.right_click(*_pixel(page, "2024-05-01T00:00:15", 40))
        page.settle()
        saved = _saved(viewer_settings)
        assert saved.features["geometry"].tolist() == ["polygon"]
        assert saved.annotations["distance"].round().tolist() == [20, 20, 60]

        page.js("setMode('select')")
        edge = _pixel(page, "2024-05-01T00:00:15", 20)  # on its lower side
        page.drag(edge, (edge[0], edge[1] - 80))
        page.settle()
        moved = _saved(viewer_settings).annotations["distance"]
        assert (moved.diff().dropna().round().tolist()) == [0, 40]  # same shape
        assert moved.iloc[0] > 25  # carried up the plot
        assert page.js("S.sel.id") == saved.features["id"].iloc[0]

    def test_drawing_snaps_to_vertices(self, page, viewer_settings):
        """A click near the first corner closes a polygon; near a vertex, joins it."""
        _new_set(page)
        page.js("setMode('polygon')")
        corners = (("00:00:10", 20), ("00:00:20", 20), ("00:00:20", 60))
        for when, distance in corners:
            page.click(*_pixel(page, f"2024-05-01T{when}", distance))
        x, y = _pixel(page, "2024-05-01T00:00:10", 20)
        page.click(x + 4, y - 4)  # close to where it began
        assert page.js("S.draft") is None
        page.settle()
        saved = _saved(viewer_settings)
        assert saved.features["geometry"].tolist() == ["polygon"]
        assert saved.annotations["distance"].round().tolist() == [20, 20, 60]

        page.js("setMode('path')")
        x, y = _pixel(page, "2024-05-01T00:00:20", 60)
        page.click(x + 5, y + 3)  # near a corner of the polygon
        corner = page.js("activeSet().feats[0].pts[2].slice(0, 2)")
        assert page.js("S.draft.pts[0].slice(0, 2)") == corner
        page.click(x + 60, y + 30)  # near nothing: where it was clicked
        assert page.js("S.draft.pts.length") == 2
        assert page.js("S.draft.pts[1][1]") < corner[1]  # lower on the page

    def test_vertices_are_added_and_removed(self, page, viewer_settings):
        """A double-click on a side adds a vertex; Delete over one removes it."""
        _new_set(page)
        page.js("setMode('path')")
        for when, distance in (("00:00:10", 20), ("00:00:30", 20)):
            page.click(*_pixel(page, f"2024-05-01T{when}", distance))
        page.key("Enter", 13)
        page.settle()
        page.js("setMode('select')")
        middle = _pixel(page, "2024-05-01T00:00:20", 20)
        page._mouse("mouseMoved", *middle)
        for count in (1, 2):
            page._mouse("mousePressed", *middle, button="left", clickCount=count)
            page._mouse("mouseReleased", *middle, button="left", clickCount=count)
        page.settle()
        times = _saved(viewer_settings).annotations["time"]
        assert [x.round("s").second for x in times] == [10, 20, 30]

        page._mouse("mouseMoved", *middle)  # over the new vertex
        page.key("Delete", 46)
        page.settle()
        times = _saved(viewer_settings).annotations["time"]
        assert [x.round("s").second for x in times] == [10, 30]

    def test_window_picks_several_and_delete_removes_them(self, page, viewer_settings):
        """A drag on empty space picks what it catches; Delete removes them."""
        _new_set(page)
        page.js("setMode('point')")
        for when in ("00:00:40", "00:00:44", "00:00:55"):
            page.click(*_pixel(page, f"2024-05-01T{when}", 100))
        page.settle()
        assert len(_saved(viewer_settings).features) == 3
        page.js("setMode('select')")
        page.drag(
            _pixel(page, "2024-05-01T00:00:37", 115),
            _pixel(page, "2024-05-01T00:00:47", 85),
        )
        assert page.js("S.many.length") == 2 and page.js("S.sel") is None
        panel = page.js("document.getElementById('selected').textContent")
        assert "2 annotations" in panel
        assert page.js("document.getElementById('btnAreaOf').disabled") is False
        page.key("Delete", 46)
        page.settle()
        left = _saved(viewer_settings).annotations["time"]
        assert [x.round("s").second for x in left] == [55]
        assert page.js("S.many.length") == 0

        # a window over another user's annotation picks it but can't delete it
        page.drag(
            _pixel(page, "2024-05-01T00:00:02", 40),
            _pixel(page, "2024-05-01T00:00:08", 20),
        )
        assert page.js("S.sel.set") == "detector/auto"
        page.key("Delete", 46)
        page.settle()
        assert page.js("findSet('detector/auto').feats.length") == 1

    def test_first_drawing_makes_a_set_and_labels_repeat(self, page, viewer_settings):
        """With no set, drawing makes one; a label typed once is offered again."""
        assert page.js("S.active") is None
        page.js("setMode('point')")
        page.settle()
        assert page.js("S.active") == "me/annotations"
        page.click(*_pixel(page, "2024-05-01T00:00:40", 100))
        assert page.js("document.activeElement.id") == "labelBox"
        page.type("vehicle")
        page.key("Enter", 13)
        assert page.js("document.activeElement === cv")
        page.click(*_pixel(page, "2024-05-01T00:00:50", 100))
        assert page.js("document.getElementById('labelBox').value") == "vehicle"
        page.key("Enter", 13)  # the same again: one key
        page.click(*_pixel(page, "2024-05-01T00:00:55", 100))
        page.key("Escape", 27)  # this one is left without a label
        page.settle()
        features = _saved(viewer_settings, "annotations").features
        assert features["label"].fillna("").tolist() == ["vehicle", "vehicle", ""]

    def test_typing_in_the_panel_survives_a_save(self, page, viewer_settings):
        """A save finishing does not rebuild the field being typed in."""
        _new_set(page)
        page.js("setMode('point')")
        page.click(*_pixel(page, "2024-05-01T00:00:40", 100))
        page.key("Escape", 27)
        page.js("setMode('select')")
        page.settle()
        note = "document.querySelector('#selected input[data-col=note]')"
        page.js(f"{note}.focus()")
        page.type("half")
        label = "document.querySelector('#selected input[data-col=label]')"
        page.js(  # another edit and its save arrive while the note is still being typed
            f"(() => {{ const e = {label}; e.value = 'car';"
            " e.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        page.wait("S.sets.every(s => !s.dirty && !s.saving)")
        assert page.js("document.activeElement.dataset.col") == "note"
        assert page.js(f"{note}.value") == "half"
        page.type(" typed")
        page.js(f"{note}.dispatchEvent(new Event('change', {{bubbles: true}}))")
        page.settle()
        saved = _saved(viewer_settings).features
        assert saved["label"].tolist() == ["car"]
        assert saved["note"].tolist() == ["half typed"]

    def test_a_column_carries_to_others_of_its_kind(self, page, viewer_settings):
        """A column added to one point is offered on the next point, not a box."""
        _new_set(page)
        page.js("setMode('point')")
        page.click(*_pixel(page, "2024-05-01T00:00:40", 100))
        page.key("Escape", 27)
        page.js(
            "(() => { const e = document.querySelector('#selected [data-newcol]');"
            " e.value = 'speed';"
            " e.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        speed = "document.querySelector('#selected input[data-col=speed]')"
        assert page.js(f"{speed} !== null")
        page.click(*_pixel(page, "2024-05-01T00:00:50", 100))  # a second point
        page.key("Escape", 27)
        assert page.js("activeSet().feats.length") == 2
        assert page.js(f"{speed}.value") == ""
        page.js(
            f"(() => {{ const e = {speed}; e.value = '12';"
            " e.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        page.js("setMode('box')")
        page.drag(
            _pixel(page, "2024-05-01T00:01:00", 60),
            _pixel(page, "2024-05-01T00:01:10", 80),
        )
        page.key("Escape", 27)
        assert page.js("selected().f.kind") == "box"
        assert page.js(f"{speed}") is None
        page.settle()
        saved = _saved(viewer_settings).features
        assert saved["speed"].iloc[1] == "12"

    def test_copy_several_at_once(self, page, viewer_settings):
        """A window over another user's crowd copies them all with one key."""
        page.js("setMode('select')")
        page.drag(
            _pixel(page, "2024-05-01T00:03:25", 115),
            _pixel(page, "2024-05-01T00:03:36", 90),
        )
        assert page.js("S.many.length") == 30
        page.key("c", 67)
        page.settle()
        features = _saved(viewer_settings, "annotations").features
        assert len(features) == 30
        assert features["derived_from"].str.startswith("detector/many/pick_").all()
        assert page.js("S.many.every(r => r.set === 'me/annotations')")
        page.key("z", 90, modifiers=2)  # one step undoes them all
        page.settle()
        assert page.js("activeSet().feats.length") == 0

    def test_copy_from_another_set(self, page, viewer_settings):
        """A feature of a read-only set is copied, recording where it came from."""
        _new_set(page)
        assert page.js("findSet('detector/auto').editable") is False
        page.click(*_pixel(page, "2024-05-01T00:00:05", 30))
        assert page.js("S.sel.set") == "detector/auto"
        page.js("document.getElementById('btnCopy').click()")
        page.settle()
        features = _saved(viewer_settings).features
        assert features["derived_from"].tolist() == ["detector/auto/auto_1"]
        assert features["derived_revision"].tolist() == [1]
        assert features["label"].tolist() == ["detected"]
        untouched = dc.annotations(viewer_settings.annotations.path / "detector/auto")
        assert len(untouched.features) == 1


class TestDownloading:
    """A dragged area becomes a request to the data API."""

    def test_area_download_link(self, page, served):
        """The link of a selected area downloads that part of the raw product."""
        page.js("setMode('area')")
        start = _pixel(page, "2024-05-01T00:00:04", 20)
        page.drag(start, _pixel(page, "2024-05-01T00:00:12", 80))
        page.wait("document.getElementById('lnkArea') !== null")
        assert "Download" in page.js("document.getElementById('lnkArea').textContent")
        link = page.js("document.getElementById('lnkArea').href")
        assert "product=" in link and "format=dasdae" in link
        with urllib.request.urlopen(link) as reply:
            body = reply.read()
        assert body[:8] == b"\x89HDF\r\n\x1a\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "area.h5"
            path.write_bytes(body)
            patch = dc.spool(path)[0]
        low, high = patch.get_coord("distance").min(), patch.get_coord("distance").max()
        assert 18 <= low <= 22 and 78 <= high <= 82
        assert patch.attrs.tag == "raw"
        start, end = (
            str(patch.get_coord("time").min()),
            str(patch.get_coord("time").max()),
        )
        assert (
            start.startswith("2024-05-01T00:00:0")
            and "00:00:03" <= start[11:19] <= "00:00:04"
        )
        assert "00:00:11" <= end[11:19] <= "00:00:13"


class TestLimits:
    """A view which would read too much is outlined instead of loaded."""

    @pytest.fixture
    def viewer_settings(self, viewer_settings):
        """Settings whose limit for one screen is a few megabytes."""
        limits = Limits(max_view_bytes=3_500_000)
        return viewer_settings.model_copy(update={"limits": limits})

    def test_pinned_level_over_the_limit_is_outlined(self, page):
        """Pinning the raw product on the whole recording asks for no tiles."""
        assert page.js("activeLevel() && S.blocked") is None
        page.js("S.pin = S.levels[1].p.id; redraw()")
        page.settle()
        assert page.js("activeLevel() && levelName(S.blocked.p)") == "raw"
        assert page.js("[...tiles.values()].filter(t => t.pid === S.pin).length") == 0
        x, y = _pixel(page, "2024-05-01T00:00:10", 60)
        for _ in range(12):
            page.wheel(x, y, -200)
        page.settle()
        assert page.js("activeLevel() && S.blocked") is None
        assert page.js(
            "[...tiles.values()].some(t => t.pid === S.pin && t.st === 'ok')"
        )

    def test_coarser_level_stands_in(self, page):
        """Between the levels, the coarse one is drawn rather than too much raw."""
        x, y = _pixel(page, "2024-05-01T00:00:30", 60)
        for _ in range(2):  # until a pixel is finer than the coarse step
            page.wheel(x, y, -200)
        page.settle()
        assert page.js("S.view.spp < S.levels[0].sx")
        assert page.js("levelName(activeLevel().p)") == "max_100ms"
        assert page.js("S.blocked") is None


class TestCrowding:
    """Features too close to tell apart are drawn as one counted marker."""

    def test_crowd_is_counted_and_opens(self, page):
        """Thirty picks are one marker; hovering counts them, a click zooms in."""
        crowd = "S.clusters.find(c => c.set === 'detector/many')"
        assert page.js(f"{crowd}.feats.length") == 30
        x, y = page.js(
            f"(() => {{ const c = {crowd}, r = cv.getBoundingClientRect();"
            " return [c.x + r.left, c.y + r.top]; })()"
        )
        page._mouse("mouseMoved", x, y)
        assert "30 annotations" in page.js("document.getElementById('tip').textContent")
        before = page.js("S.view.spp")
        page.click(x, y)
        page.settle()
        assert page.js("S.view.spp") < before / 10
        # spread over the view, the picks now stand alone or in small crowds
        assert page.js("Math.max(0, ...S.clusters.map(c => c.feats.length))") < 20
        # and the view is on them, not somewhere empty
        middle = "px2x(plotRect().x + plotRect().w / 2)"
        assert page.js(f"Math.abs({middle} - parseTime('2024-05-01T00:03:30'))") < 2

    def test_download_around_an_annotation(self, page):
        """A selected annotation's download button sets the area to its box."""
        page.click(*_pixel(page, "2024-05-01T00:00:05", 30))
        page.js("document.getElementById('btnAreaOf').click()")
        page.wait("document.getElementById('lnkArea') !== null")
        area = page.js("S.area")
        when = page.js("parseTime('2024-05-01T00:00:05')")
        # a point has no extent, so the area is padded round it
        assert area["x0"] < when < area["x1"] and area["y0"] < 30 < area["y1"]
        assert "product=" in page.js("document.getElementById('lnkArea').href")

    def test_download_the_window(self, page):
        """ "window" takes what is on screen; Sync waits for a local directory."""
        page.js("document.getElementById('btnAreaWindow').click()")
        page.wait("document.getElementById('lnkArea') !== null")
        area, view = page.js("[S.area, windowBounds()]")
        assert area == view
        assert area["x1"] > area["x0"] and area["y1"] > area["y0"]
        assert page.js("document.getElementById('btnSync').disabled") is True
        assert page.js("document.getElementById('btnAreaOf').disabled") is True
        # the finest product is ticked; ticking another downloads both in one file
        boxes = "[...document.querySelectorAll('#areaProducts input')]"
        assert page.js(f"{boxes}.map(b => b.checked).filter(Boolean).length") == 1
        assert page.js(f"{boxes}.length") > 1
        page.js(f"{boxes}.find(b => !b.checked).click()")
        link = "(document.getElementById('lnkArea') || {href: ''}).href"
        page.wait(f"{link}.split('product=').length === 3")
        for _ in range(2):  # one at a time: each change redraws the boxes
            page.js("document.querySelector('#areaProducts input:checked').click()")
        size = "document.getElementById('areaSize').textContent"
        page.wait(f"{size} === 'choose a product'")
        page.js("document.getElementById('btnAreaClear').click()")
        assert page.js("S.area") is None
        assert page.js("document.getElementById('lnkArea')") is None
