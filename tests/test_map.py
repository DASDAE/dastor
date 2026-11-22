"""Tests for the viewer's map: the fibre's layout from the spool's inventory."""

import json
import shutil

import dascore as dc
import pytest
from conftest import VIEW_KEY
from fastapi.testclient import TestClient

from dastor import mapview
from dastor.app import create_app
from dastor.cli import browse_settings
from dastor.config import Settings

GEOGRAPHIC = (
    "  authority: EPSG\n  code: '4326'\n  name: WGS 84\n"
    "  coordinate_labels: [longitude, latitude]\n  units: [degree, degree]\n"
)
LOCAL = (
    "  authority: LOCAL\n  code: site\n  name: site grid\n"
    "  coordinate_labels: [x, y, z]\n  units: [m, m, m]\n"
)
GEOMETRY = {
    GEOGRAPHIC: (
        "name,distance,longitude,latitude,chainage (m)\n"
        "straight,0,-7.0,53.0,0\nstraight,60,-6.999,53.0,60\n"
        "coil,60,-6.999,53.0,60\ncoil,100,-6.999,53.0,60\n"
        "straight on,100,-6.999,53.0,60\nstraight on,126,-6.9986,53.0,86\n"
    ),
    LOCAL: (
        "name,distance,x,y,z\n"
        "straight,0,0,0,5\nstraight,60,60,0,6\n"
        "coil,60,60,0,6\ncoil,100,60,0,6\n"
        "straight on,100,60,0,6\nstraight on,126,60,26,7\n"
    ),
}


def write_inventory(root, crs=GEOGRAPHIC):
    """Write an inventory describing the viewer's recording into a directory."""
    network, array, _, code = VIEW_KEY.split(".")
    path = root / "fiber_arrays" / f"{network}.{array}" / "path"
    path.mkdir(parents=True)
    (root / "networks").mkdir()
    (root / "acquisitions").mkdir()
    (root / "inventory.yaml").write_text(
        "object_type: Inventory\ndescription: A test inventory.\n"
        f"coordinate_reference_system:\n{crs}"
    )
    (root / "networks" / f"{network}.yaml").write_text(
        "object_type: Network\nname: Demo\n"
    )
    (path.parent / "attrs.yaml").write_text(
        "object_type: FiberArray\nname: The demo cable\ndescription: An array.\n"
    )
    (path / "attrs.yaml").write_text(
        "object_type: OpticalPath\nname: fibre\n"
        "description: Positions are paced out, good to a metre.\n"
    )
    (path / "optical_components.csv").write_text(
        "object_type,distance_min,distance_max,name\nFiberSegment,0,126,fibre\n"
    )
    (path / "geometry.csv").write_text(GEOMETRY[crs])
    (path / "labels.csv").write_text(
        "distance_min,distance_max,group,value\n0,50,ground,clay\n50,126,ground,sand\n"
    )
    (path / "coupling.csv").write_text(
        "distance_min,distance_max,coupling_type\n60,100,coiled\n"
    )
    (root / "acquisitions" / f"{VIEW_KEY}.yaml").write_text(
        "object_type: Acquisition\ndata_category: DAS\n"
        f"description: The {code} recording.\n"
        "time_min: '2024-05-01T00:00:00'\ntime_max: '2024-05-02T00:00:00'\n"
        "distance_map:\n  instrument_distance: [0, 126]\n  distance: [0, 126]\n"
    )
    dc.inventory(root).check()
    return root


def make_client(data, tmp_path, **map_config):
    """Return a test client serving a data directory, with a map section."""
    config = browse_settings(str(data), str(tmp_path / "annotations"))
    config["map"] = map_config
    return TestClient(create_app(Settings.model_validate(config)))


@pytest.fixture
def data(viewer_dir, tmp_path):
    """A copy of the viewer's recording, to put an inventory beside."""
    out = tmp_path / "data"
    shutil.copytree(viewer_dir, out)
    return out


@pytest.fixture
def mapped(data, tmp_path):
    """The map of a recording with a geographic inventory beside it."""
    write_inventory(data / ".inventory")
    with make_client(data, tmp_path) as client:
        yield client.get("/v1/viewer/map").json()


class TestLayout:
    """Tests for what the map is told about the fibre."""

    def test_the_fibre_comes_from_the_inventory(self, mapped):
        """The path has its position, labels, coupling, columns and notes."""
        assert mapped["inventory"] is True
        assert mapped["frame"]["kind"] == "geographic"
        (path,) = mapped["paths"]
        assert path["id"] == "DEMO.CABLE."
        assert path["name"] == "The demo cable"
        assert path["x"][0] == -7.0 and path["y"][0] == 53.0
        assert path["tracks"]["ground"] == [[0, 50, "clay"], [50, 126, "sand"]]
        assert path["tracks"]["coupling"] == [[60, 100, "coiled"]]
        assert path["columns"]["chainage"] == {
            "units": "m",
            "v": [0, 60, 60, 60, 60, 86],
        }
        assert path["about"].startswith("Positions are paced out")

    def test_segments_are_kept_apart(self, mapped):
        """Each geometry segment starts at a break, so none runs into the next."""
        (path,) = mapped["paths"]
        assert path["d"] == [0, 60, 60, 100, 100, 126]
        assert path["breaks"] == [0, 2, 4]

    def test_the_recording_joins_by_its_key(self, mapped):
        """The recording is listed with its distance map and its notes."""
        (recording,) = mapped["paths"][0]["recordings"]
        assert recording["key"] == VIEW_KEY
        assert recording["from"] == [0, 126] and recording["to"] == [0, 126]
        assert recording["about"] == "The A1 recording."

    def test_a_local_frame_is_a_plane(self, data, tmp_path):
        """Coordinates which are not longitude and latitude are drawn as they are."""
        write_inventory(data / ".inventory", LOCAL)
        with make_client(data, tmp_path) as client:
            mapped = client.get("/v1/viewer/map").json()
        frame = mapped["frame"]
        assert (frame["kind"], frame["labels"], frame["units"]) == (
            "plane",
            ["x", "y"],
            "m",
        )
        (path,) = mapped["paths"]
        assert path["x"] == [0, 60, 60, 60, 60, 60]
        assert path["y"] == [0, 0, 0, 0, 0, 26]
        assert path["columns"]["z"] == {"units": "m", "v": [5, 6, 6, 6, 6, 7]}

    def test_data_the_inventory_does_not_hold_is_not_drawn(self, data, tmp_path):
        """The join is the acquisition key: another key's fibre is not shown."""
        root = write_inventory(data / ".inventory")
        old = root / "acquisitions" / f"{VIEW_KEY}.yaml"
        old.rename(old.with_name("DEMO.CABLE..B2.yaml"))
        with make_client(data, tmp_path) as client:
            mapped = client.get("/v1/viewer/map").json()
        assert mapped["inventory"] is True and mapped["paths"] == []


class TestWithoutAnInventory:
    """Tests that the viewer works as before when there is nothing to draw."""

    def test_no_inventory(self, viewer):
        """A spool without an inventory has no map, and says why."""
        mapped = viewer.get("/v1/viewer/map").json()
        assert mapped["inventory"] is False and mapped["paths"] == []
        assert mapped["reason"].startswith("No inventory")

    def test_a_broken_inventory_does_not_stop_the_viewer(self, data, tmp_path):
        """An inventory which cannot be read turns the map off, nothing else."""
        (data / ".inventory").mkdir()
        (data / ".inventory" / "inventory.yaml").write_text("object_type: [")
        with make_client(data, tmp_path) as client:
            mapped = client.get("/v1/viewer/map").json()
            assert mapped["paths"] == [] and "could not be read" in mapped["reason"]
            assert client.get("/v1/viewer/products").json()["products"]


class TestVisibility:
    """Tests for who is shown a fibre."""

    PATHS = [{"id": "N.A.", "recordings": [{"key": "N.A..R1"}, {"key": "N.A..R2"}]}]  # noqa: RUF012

    def test_a_fibre_is_shown_through_a_whole_acquisition(self):
        """Reading some files of an acquisition does not show where it was."""
        assert mapview.visible(self.PATHS, {"N.A..R1"}, frozenset()) == []
        (path,) = mapview.visible(self.PATHS, {"N.A..R1"}, frozenset({"N.A..R1"}))
        assert path["recordings"] == [{"key": "N.A..R1"}]


class TestBackground:
    """Tests for the background layers and the web map."""

    @pytest.fixture
    def layers(self, tmp_path):
        """A layers directory: one layer, a link out of it, and another file."""
        root = tmp_path / "layers"
        root.mkdir()
        drawing = {"type": "FeatureCollection", "features": []}
        (root / "geology.geojson").write_text(json.dumps(drawing))
        (tmp_path / "outside.geojson").write_text(json.dumps(drawing))
        (root / "linked.geojson").symlink_to(tmp_path / "outside.geojson")
        (root / "notes.txt").write_text("not a layer")
        return root

    def test_layers_are_the_directorys_own_geojson(self, viewer_dir, tmp_path, layers):
        """Only GeoJSON files lying in the directory itself are layers."""
        with make_client(viewer_dir, tmp_path, layers=str(layers)) as client:
            assert client.get("/v1/viewer/map").json()["layers"] == ["geology"]
            reply = client.get("/v1/viewer/map/layer/geology")
            assert reply.json()["type"] == "FeatureCollection"
            for name in ("linked", "notes", "nothing", "..%2Foutside"):
                assert client.get(f"/v1/viewer/map/layer/{name}").status_code == 404

    def test_the_web_map_is_off_unless_configured(self, viewer_dir, tmp_path):
        """No tile address is given to the page unless the config states one."""
        with make_client(viewer_dir, tmp_path) as client:
            assert client.get("/v1/viewer/map").json()["basemap"] is None
        url = "https://tiles.example/{z}/{x}/{y}.png"
        with make_client(viewer_dir, tmp_path, basemap_url=url) as client:
            assert client.get("/v1/viewer/map").json()["basemap"]["url"] == url
