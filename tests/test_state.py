# SPDX-License-Identifier: Apache-2.0
"""The since-state file: round trip, atomic write, corrupt files refuse instead of resetting."""

import json
import os
from pathlib import Path

import pytest
from horae_sdk import PluginError

from horae_imap.state import FolderState, load, save


def test_a_missing_file_is_an_empty_state(tmp_path: Path) -> None:
    assert load(str(tmp_path / "none.json")) == {}


def test_round_trip_is_private_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = {"work": {"INBOX": FolderState(7, 42), "Lists/School": FolderState(1, 0)}}
    save(str(path), state)
    assert load(str(path)) == state
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    save(str(path), {})
    assert load(str(path)) == {}


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"version": 2, "accounts": {}}',
        b'{"version": 1}',
        b'{"version": 1, "accounts": {"a": {"INBOX": {"uidvalidity": "1", "last_uid": 1}}}}',
        b'{"version": 1, "accounts": {"a": {"INBOX": {"uidvalidity": 1}}}}',
        b'{"version": 1, "accounts": {"a": {"INBOX": {"uidvalidity": -1, "last_uid": 1}}}}',
        b'{"version": 1, "accounts": {"a": {"INBOX": {"uidvalidity": true, "last_uid": 1}}}}',
        b'{"version": 1, "accounts": []}',
        b"x" * (1024 * 1024 + 2),
    ],
)
def test_a_corrupt_state_refuses_and_is_left_alone(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content)
    with pytest.raises(PluginError, match="state_invalid"):
        load(str(path))
    assert path.read_bytes() == content


def test_an_unreadable_or_unwritable_path_raises_a_fixed_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="state_unreadable"):
        load(str(tmp_path))  # a directory
    with pytest.raises(PluginError, match="state_unwritable"):
        save(str(tmp_path / "missing-dir" / "state.json"), {})
    assert json.dumps({}) == "{}"
