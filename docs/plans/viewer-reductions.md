# Plan: reductions and scales for the viewer

Ideas taken from Datashader and HoloViews after comparing them with dastor's tiles (November 2026). Neither library is to be adopted: Datashader's strength is aggregating scattered points and lines, where dastor reduces a regular grid with a few lines of NumPy, and HoloViews needs a Python session for each viewer, which does not fit a stateless API whose every tile passes the access check. None of this is started.

## Background: the two reductions

- **Levels** are products stored in the spool (`raw`, `max_1s`, ...), made ahead of time. They decide what is read from disk.
- **The tile reduction** runs on the server for each tile: the level's samples are binned to the tile's pixels with the statistic chosen in the page (`max`, `mean`, `absmax`). Nothing is stored.

The viewer does not know how a level was made, so a statistic may not match it: `mean` over a `max` level is a mean of maxes, not the mean of the raw data.

## 1. A signed-extreme statistic

Keep, for each pixel, the value of largest magnitude with its sign.

- **Why:** for signed data (strain rate), `max` favours the positive side, `mean` cancels, and `absmax` loses the sign. This is the statistic which suits the diverging colour maps.
- **Where:** `stat=extreme` in `tiles.render_tile` (`_reduce_axis`), beside the others; one more option in the page's statistic menu.
- **Check:** as with the others, the result must not depend on how the data is split into files.
- **Levels:** a builder of coarse products should offer the same reduction, so a coarse level of signed data keeps its sign.

## 2. A histogram-equalised colour scale

A third scale beside linear and log, which spreads the colours by how common values are (Datashader's `eq_hist`).

- **Why:** a few loud events flatten everything else on a linear scale.
- **Where:** the browser only. The lookup is built from the values on screen, as auto contrast is; the colour bar's ticks become uneven and should say so.
- **Open question:** whether the mapping is recomputed on every pan (colours shift as you move) or held until asked for, as manual limits are.

## 3. Reading the patches of a tile in parallel

- **Why:** the measured cost of a tile is not the binning but a fixed cost of about 14 ms for each patch opened, plus disk reads.
- **Where:** `tiles.render_tile`, reading and reducing patches in threads, within `limits.max_concurrent_loads`.
- **When:** once it can be measured on the real server. Related: a tile the browser has abandoned still runs to the end on the server.

## 4. A command which builds coarse levels

Superseded by `config-and-pyramids.md`.


Already agreed: `dastor` may build generic reductions (max, mean, count, and the signed extreme of item 1) as products of the same recording, told apart by `tag`. It waits on DASCore's Zarr append. Datashader's `render_tiles` is the same idea and adds nothing to design.

## Order

1. Signed extreme (item 1): small, and fixes a real weakness for signed data.
2. Equalised scale (item 2).
3. Parallel reads (item 3), with measurements from the server.
4. The level builder (item 4), after Zarr append.
