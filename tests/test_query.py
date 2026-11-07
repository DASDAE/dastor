import pytest

from dastor.query import QueryError, selection


def test_bounds_and_attrs():
    params = [
        ("time_min", "2020-01-03T00:00:04"),
        ("distance_max", "20"),
        ("distance_min", "1.5"),
        ("tag", "public*"),
        ("format", "json"),
    ]
    assert selection(params, reserved={"format"}) == {
        "time": ("2020-01-03T00:00:04", None),
        "distance": (1.5, 20),
        "tag": "public*",
    }


def test_attribute_values_stay_text():
    assert selection([("station", "10")]) == {"station": "10"}


@pytest.mark.parametrize(
    "params",
    [[("tag", "a"), ("tag", "b")], [("time", "x"), ("time_min", "1")]],
)
def test_ambiguous_queries_raise(params):
    with pytest.raises(QueryError):
        selection(params)
