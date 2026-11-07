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
        return {"spools": {"a": {**base, "rules": rules}}}

    bad = [
        {"spools": {"a/b": base}},
        rule("(", ["*"]),
        rule("", ["alice"]),
        {"spools": {"a": {**base, "typo": 1}}},
        {"spools": {}, "users": [{"name": "a", "key_sha256": "0" * 64}] * 2},
    ]
    for value in bad:
        with pytest.raises(ValidationError):
            Settings.model_validate(value)


def test_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"spools:\n  das:\n    path: {tmp_path}\n")
    assert Settings.from_yaml(path).spools["das"].path == tmp_path


def test_new_key_prints_matching_digest(capsys):
    main(["new-key"])
    lines = dict(x.split(": ", 1) for x in capsys.readouterr().out.splitlines())
    assert hash_key(lines["key"].strip()) == lines["key_sha256"].strip()
