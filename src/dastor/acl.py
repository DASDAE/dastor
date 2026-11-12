"""File-level access control over spool contents."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import dascore as dc
import numpy as np
import pandas as pd

from dastor.auth import Principal
from dastor.config import Rule


def missing_columns(contents: pd.DataFrame, rules: Sequence[Rule]) -> list[str]:
    """Return the rule columns the contents lack."""
    return sorted({x.column for x in rules} - set(contents.columns))


def matching_rows(
    contents: pd.DataFrame, rules: Sequence[Rule], principal: Principal
) -> np.ndarray:
    """
    Return a boolean mask of the rows some rule grants the principal.

    A row matches when a rule granting the principal finds its pattern in
    the rule's column. A rule whose column is missing matches nothing, so a
    mistake fails closed.
    """
    matched = pd.Series(False, index=contents.index)
    for rule in rules:
        if rule.column not in contents.columns or not principal.granted(rule.allow):
            continue
        text = contents[rule.column].astype("string").fillna("")
        matched |= text.str.contains(re.compile(rule.pattern), regex=True)
    return matched.to_numpy(dtype=bool)


def visible_files(
    contents: pd.DataFrame, rules: Sequence[Rule], principal: Principal
) -> frozenset[str]:
    """
    Return the source paths of the files a principal may see.

    Access is per file: a file is visible only when all its rows match, so
    a file is never partly served.
    """
    matched = pd.Series(matching_rows(contents, rules, principal))
    paths = contents["source_path"].astype(str).to_numpy()
    whole = matched.groupby(paths, sort=False).all()
    return frozenset(whole.index[whole.to_numpy()])


def whole_acquisitions(contents: pd.DataFrame, files: frozenset[str]) -> frozenset[str]:
    """
    Return the acquisition keys all of whose files are among `files`.

    Patches naming no acquisition share the key "". What describes an
    acquisition, such as an annotation set, may be shown only to those who
    may read every file of it: seeing one file of a recording says nothing
    about the rest.
    """
    if contents.empty:
        return frozenset()
    if "acquisition_key" in contents.columns:
        keys = contents["acquisition_key"].astype("string").fillna("").astype(str)
    else:
        keys = pd.Series("", index=contents.index)
    seen = contents["source_path"].astype(str).isin(files)
    whole = seen.groupby(keys.to_numpy(), sort=False).all()
    return frozenset(whole.index[whole.to_numpy()])


def patch_matches(
    patch: dc.Patch, row: Mapping, rules: Sequence[Rule], principal: Principal
) -> bool:
    """
    Return True if a loaded patch still matches the rules.

    The patch's own attributes stand in for the indexed row's, so a file
    changed since it was indexed is judged by what it now holds.
    """
    attrs = patch.attrs.model_dump()
    values = dict(row)
    values.update({x.column: attrs[x.column] for x in rules if x.column in attrs})
    return bool(matching_rows(pd.DataFrame([values]), rules, principal)[0])
