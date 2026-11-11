import pytest
from pydantic import ValidationError

from dastor.auth import ANONYMOUS, Authenticator, InvalidKeyError, Principal, hash_key
from dastor.cli import main
from dastor.config import Settings, User


def test_anonymous_without_header():
    assert Authenticator([])(None) is ANONYMOUS


def test_bearer_key_maps_to_user():
    users = [User(name="alice", key_sha256=hash_key("k"), groups=["lab"])]
    out = Authenticator(users)("Bearer k")
    assert out == Principal("alice", frozenset({"lab"}), True)


@pytest.mark.parametrize("header", ["Bearer nope", "Basic k", "Bearer "])
def test_bad_header_raises(header):
    with pytest.raises(InvalidKeyError):
        Authenticator([User(name="a", key_sha256=hash_key("k"))])(header)


def test_grants():
    alice = Principal("alice", frozenset({"lab"}), True)
    assert alice.granted(["group:lab"])
    assert alice.granted(["user:alice"])
    assert not alice.granted(["user:bob", "group:other"])
    assert ANONYMOUS.granted(["*"])
    # An unauthenticated principal never matches by name.
    assert not ANONYMOUS.granted(["user:anonymous"])


def test_config_rejects_bad_values(tmp_path):
    base = {"path": str(tmp_path)}

    def rule(pattern, allow):
        rules = [{"column": "x", "pattern": pattern, "allow": allow}]
        return {"spool": {**base, "rules": rules}}

    bad = [
        rule("(", ["*"]),
        rule("", ["alice"]),
        {"spool": {**base, "typo": 1}},
        {"spool": base, "users": [{"name": "a", "key_sha256": "0" * 64}] * 2},
        {"spools": {"a": base}},
    ]
    for value in bad:
        with pytest.raises(ValidationError):
            Settings.model_validate(value)


def test_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"spool:\n  path: {tmp_path}\n")
    assert Settings.from_yaml(path).spool.path == tmp_path


def test_new_key_prints_matching_digest(capsys):
    main(["new-key"])
    lines = dict(x.split(": ", 1) for x in capsys.readouterr().out.splitlines())
    assert hash_key(lines["key"].strip()) == lines["key_sha256"].strip()


def test_annotation_owners_are_plain_names(tmp_path):
    """With annotations on, owners must be names a folder can have."""
    base = {"spool": {"path": str(tmp_path)}}
    digest = hash_key("key")
    store = {"path": str(tmp_path / "annotations")}
    odd = {"users": [{"name": "alice@example.org", "key_sha256": digest}]}
    Settings.model_validate({**base, **odd})  # fine while nothing is stored
    with pytest.raises(ValidationError, match="plain words"):
        Settings.model_validate({**base, **odd, "annotations": store})
    plain = {"users": [{"name": "alice", "key_sha256": digest}]}
    with pytest.raises(ValidationError, match="name of a user"):
        store_as = {**store, "anonymous_owner": "alice"}
        Settings.model_validate({**base, **plain, "annotations": store_as})
    with pytest.raises(ValidationError, match="not a plain name"):
        store_as = {**store, "anonymous_owner": "../etc"}
        Settings.model_validate({**base, "annotations": store_as})
    Settings.model_validate({**base, **plain, "annotations": store})
