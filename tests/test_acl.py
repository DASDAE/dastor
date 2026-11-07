import dascore as dc
import pandas as pd

from dastor.acl import matching_rows, missing_columns, patch_matches, visible_files
from dastor.auth import ANONYMOUS, Principal
from dastor.config import Rule

LAB = Principal("alice", frozenset({"lab"}), True)
RULES = [
    Rule(column="tag", pattern="^public", allow=["*"]),
    Rule(column="tag", pattern=".", allow=["group:lab"]),
]
CONTENTS = pd.DataFrame(
    {
        "source_path": ["a.h5", "b.h5", "b.h5", "c.h5"],
        "tag": ["public", "public", "secret", "secret"],
    }
)


def test_rules_grant_rows_and_files():
    assert matching_rows(CONTENTS, RULES, LAB).all()
    assert visible_files(CONTENTS, RULES, LAB) == {"a.h5", "b.h5", "c.h5"}


def test_mixed_file_hidden_entirely():
    assert matching_rows(CONTENTS, RULES, ANONYMOUS).tolist() == [
        True,
        True,
        False,
        False,
    ]
    assert visible_files(CONTENTS, RULES, ANONYMOUS) == {"a.h5"}


def test_no_rules_hide_everything():
    assert visible_files(CONTENTS, [], LAB) == frozenset()


def test_missing_column_fails_closed():
    rules = [Rule(column="station", pattern=".", allow=["*"])]
    assert missing_columns(CONTENTS, rules) == ["station"]
    assert not matching_rows(CONTENTS, rules, LAB).any()


def test_missing_values_do_not_match():
    df = CONTENTS.assign(tag=[None, "public", "public", None])
    assert matching_rows(df, RULES[:1], ANONYMOUS).tolist() == [
        False,
        True,
        True,
        False,
    ]


def test_patch_attrs_override_indexed_row():
    row = {"source_path": "a.h5", "tag": "public"}
    patch = dc.get_example_patch()
    assert patch_matches(patch.update_attrs(tag="public"), row, RULES, ANONYMOUS)
    assert not patch_matches(patch.update_attrs(tag="secret"), row, RULES, ANONYMOUS)
