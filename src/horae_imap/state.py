# SPDX-License-Identifier: Apache-2.0
"""Since-state: per account label and folder, the UIDVALIDITY and the last UID handled.

A JSON file at a path the core approved for writing (`call.paths.allowed(path, write=True)`,
granted in `horae.lock`). The SDK has no state store of its own and the other plugins keep none,
so this is the plugin's only write, and only its own file: it holds two integers per folder, no
mail content and no secret.

The file is read at the start of a call and written once, at the end, after every folder was
read successfully, by an atomic replace (`os.replace` of a sibling file, mode 0600). A call that
raises leaves it as it was, so the next run retries the same mail.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from horae_sdk import PluginError

VERSION = 1
MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FolderState:
    uidvalidity: int
    last_uid: int
    """The highest UID handled: the next run fetches UIDs above it."""


def _natural(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**32 else None


def load(path: str) -> dict[str, dict[str, FolderState]]:
    """`{account: {folder: FolderState}}`; an absent file is an empty state, anything else
    unreadable raises (a corrupt state must not silently become a re-baseline)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        raise PluginError("state_unreadable: cannot read the state file") from None
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise ValueError("version")
        result: dict[str, dict[str, FolderState]] = {}
        for account, folders in document["accounts"].items():
            result[account] = {}
            for folder, entry in folders.items():
                validity, last = _natural(entry["uidvalidity"]), _natural(entry["last_uid"])
                if validity is None or last is None:
                    raise ValueError("number")
                result[account][folder] = FolderState(validity, last)
        return result
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PluginError("state_invalid: the state file is not a valid imap state") from None


def save(path: str, state: dict[str, dict[str, FolderState]]) -> None:
    """Write `state` atomically (a temporary sibling, then `os.replace`)."""
    document = {
        "version": VERSION,
        "accounts": {
            account: {
                folder: {"uidvalidity": entry.uidvalidity, "last_uid": entry.last_uid}
                for folder, entry in sorted(folders.items())
            }
            for account, folders in sorted(state.items())
        },
    }
    directory = Path(path).parent
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".imap-state-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise PluginError("state_unwritable: cannot write the state file") from None
