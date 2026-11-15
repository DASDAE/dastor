"""
Profile the viewer against a running server: where a screen's time goes.

    uv run python benchmarks/profile_viewer.py 'http://host:port/viewer/#g=...&v=...'

It measures, from this machine:

1. the link: round trip (`/health`) and throughput (a float32 tile, less the
   time the server says it took);
2. tiles one at a time: total, the server's part (its `Server-Timing`), and
   the rest, which is the network;
3. in a headless Chromium: opening the view until it is drawn, then a fast
   zoom through the levels (the wheel turned quickly), with the tiles asked
   for, given up on, and their times.

Needs Chromium (as the browser tests do). The view should hold data, on a
server open to visitors (a trusting one, say): the headless browser does not
sign in.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from test_browser import Browser  # noqa: E402  (the browser tests' harness)

SCREEN_DONE = (
    "(() => { const l = activeLevel(); if (!l || settling) return false;"
    " const [zx, zy] = zooms(l); return visibleTiles(l, zx, zy).every(([ix, iy]) =>"
    " { const t = tiles.get(tileKey(l, zx, zy, ix, iy));"
    " return t && t.st !== 'loading'; }); })()"
)


def _get(url: str, headers: dict | None = None) -> tuple[float, bytes, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    start = time.perf_counter()
    with urllib.request.urlopen(request) as reply:
        body = reply.read()
        return time.perf_counter() - start, body, dict(reply.headers)


def _server_ms(headers: dict) -> float:
    timing = headers.get("Server-Timing") or headers.get("server-timing") or ""
    return sum(float(x.split(";dur=")[1]) for x in timing.split(",") if ";dur=" in x)


def _ms(seconds: float) -> str:
    return f"{1000 * seconds:6.0f} ms"


def link(base: str, auth: dict) -> None:
    """Round trip and throughput of the link to the server."""
    trips = [_get(f"{base}/health", auth)[0] for _ in range(5)]
    print(f"round trip (median of 5): {_ms(statistics.median(trips))}")


def tiles(base: str, page: Browser, auth: dict) -> None:
    """Time a few of the view's tiles one at a time: server against network."""
    asks = page.js(
        "(() => { const l = activeLevel(), [zx, zy] = zooms(l),"
        " [tx, ty] = tileSpan(l, zx, zy);"
        " return visibleTiles(l, zx, zy).slice(0, 4).map(([ix, iy]) => query({"
        " product: l.p.id, width: TILE, height: TILE, stat: S.disp.stat,"
        " [`${S.x}_min`]: xWire(ix * tx), [`${S.x}_max`]: xWire((ix + 1) * tx),"
        " [`${S.y}_min`]: iy * ty, [`${S.y}_max`]: (iy + 1) * ty })); })()"
    )
    for i, ask in enumerate(asks):
        for precision in ("single", "half"):
            url = f"{base}/v1/viewer/tile?{ask}&precision={precision}"
            took, body, headers = _get(url, {**auth, "Accept-Encoding": "gzip"})
            server = _server_ms(headers) / 1000
            rest = took - server
            rate = len(body) / rest / 1e6 if rest > 0 else float("inf")
            print(
                f"tile {i} {precision:6s}: {_ms(took)} = server {_ms(server)}"
                f" + network {_ms(rest)}, {len(body) / 1e3:6.0f} kB"
                f" (~{rate:.1f} MB/s)"
            )


def browser(page: Browser, zoom_steps: int) -> None:
    """Open the view, then zoom fast through the levels; report both."""
    start = time.perf_counter()
    page.wait("window.dastorReady === true", 120)
    ready = time.perf_counter() - start
    page.wait(SCREEN_DONE, 300)
    drawn = time.perf_counter() - start
    page.wait("window.dastorIdle && window.dastorIdle()", 300)
    ring = time.perf_counter() - start
    print(
        f"open: page ready {_ms(ready)}, screen drawn {_ms(drawn)},"
        f" ring round it {_ms(ring)}"
    )
    _summary(page, "open")
    page.js("window.dastorStats = []")
    x, y = page.js(
        "(() => { const r = cv.getBoundingClientRect(), P = plotRect();"
        " return [r.left + P.x + P.w / 2, r.top + P.y + P.h / 2]; })()"
    )
    start = time.perf_counter()
    for _ in range(zoom_steps):  # quicker than tiles load: each level is passed by
        page.wheel(x, y, -300)
        time.sleep(0.05)
    turned = time.perf_counter() - start
    time.sleep(0.2)  # past the settling pause, so the last level's tiles are asked for
    page.wait(SCREEN_DONE, 300)
    settled = time.perf_counter() - start
    page.wait("window.dastorIdle && window.dastorIdle()", 300)
    level = page.js("levelNumber(activeLevel().p)")
    print(
        f"fast zoom ({zoom_steps} wheel steps in {_ms(turned)}):"
        f" screen drawn {_ms(settled)}"
        f" after the first step, now at level {level}"
    )
    _summary(page, "zoom")


def _summary(page: Browser, name: str) -> None:
    stats = page.js("window.dastorStats")
    done = [s for s in stats if s["status"] != "aborted"]
    aborted = len(stats) - len(done)
    if not done:
        print(f"  {name}: no tiles")
        return
    server = [sum(s["server"].values()) for s in done]
    total = [s["ms"] for s in done]
    median, server_median = statistics.median(total), statistics.median(server)
    print(
        f"  {name}: {len(done)} tiles ({aborted} given up), median {median:.0f} ms"
        f" of which server {server_median:.0f} ms, max {max(total):.0f} ms,"
        f" {sum(s['bytes'] for s in done) / 1e6:.2f} MB"
    )


def main() -> None:
    """Run the profile on the view given."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="a viewer address, with its #g=...&v=... view")
    parser.add_argument("--zoom-steps", type=int, default=12)
    args = parser.parse_args()
    split = urllib.parse.urlsplit(args.url)
    base = f"{split.scheme}://{split.netloc}{split.path.rsplit('/viewer', 1)[0]}"
    link(base, {})
    page = Browser(args.url)
    try:  # the browser is closed however the profile ends
        browser(page, args.zoom_steps)
        tiles(base, page, {})
    finally:
        page.close()
    print(json.dumps({"url": args.url}))


if __name__ == "__main__":
    main()
