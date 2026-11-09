# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake IMAP session answering from synthetic messages (never real mail),
and the settings of the GreenMail test server the integration tests talk to."""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import EllipsisType
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from horae_imap import build
from horae_imap.session import Endpoint, Selected

HERE = Path(__file__).parent
USERNAME = "synthetic-user"
PASSWORD = "synthetic-password-0123"  # noqa: S105 (not a real password)
ENV = {"IMAP_USERNAME": USERNAME, "IMAP_PASSWORD": PASSWORD}
HOST = "imap.fastmail.com"


def raw(
    sender: str = "teacher@school.example.org",
    subject: str = "Hello",
    body: str = "Nothing here.",
    *,
    day: str | None = "Mon, 02 Nov 2026 10:00:00 +0000",
    message_id: str | None = None,
    list_id: str | None = None,
) -> tuple[bytes, bytes]:
    """The header fields and the text a fetch returns for a synthetic message."""
    lines = [f"From: {sender}", f"Subject: {subject}"]
    if day is not None:
        lines.append(f"Date: {day}")
    if message_id is not None:
        lines.append(f"Message-ID: <{message_id}>")
    if list_id is not None:
        lines.append(f"List-Id: {list_id}")
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode(), body.encode()


@dataclass
class FakeFolder:
    uidvalidity: int = 1000
    messages: dict[int, tuple[bytes, bytes] | None] = field(default_factory=dict)
    uidnext: int | None = None
    """`None`: computed from the messages; `-1`: the server does not say."""


class FakeSession:
    """An in-memory mailbox that records what the plugin asked of it."""

    def __init__(self, folders: Mapping[str, FakeFolder]) -> None:
        self.folders = dict(folders)
        self.current: FakeFolder | None = None
        self.log: list[tuple[str, Any]] = []
        self.logged_out = False

    def examine(self, folder: str) -> Selected:
        self.log.append(("examine", folder))
        found = self.folders.get(folder)
        if found is None:
            from horae_sdk import PluginError

            raise PluginError(f"folder_unavailable: cannot open {folder!r}")
        self.current = found
        top = max(found.messages, default=0)
        uidnext = found.uidnext if found.uidnext is not None else top + 1
        return Selected(found.uidvalidity, None if uidnext == -1 else uidnext, len(found.messages))

    def uids(self, low: int, high: int) -> list[int]:
        assert self.current is not None
        self.log.append(("uids", (low, high)))
        return sorted(u for u in self.current.messages if low <= u <= high)

    def highest_uid(self) -> int:
        assert self.current is not None
        self.log.append(("highest", None))
        return max(self.current.messages, default=0)

    def fetch(self, uid: int, *, body_bytes: int) -> tuple[bytes, bytes] | None:
        assert self.current is not None
        self.log.append(("fetch", uid))
        found = self.current.messages.get(uid)
        return None if found is None else (found[0], found[1][:body_bytes])

    def logout(self) -> None:
        self.logged_out = True

    @property
    def fetched(self) -> list[int]:
        return [value for name, value in self.log if name == "fetch"]


class Harness:
    """A plugin wired to a fake session, a state file under `tmp_path` and a manual clock."""

    def __init__(self, tmp_path: Path, folders: Mapping[str, FakeFolder]) -> None:
        self.session = FakeSession(folders)
        self.endpoints: list[tuple[Endpoint, str, str]] = []
        self.state = tmp_path / "state.json"
        self.root = str(tmp_path)
        self.now = 0.0
        self.step = 0.0

        def connector(endpoint: Endpoint, username: str, password: str) -> FakeSession:
            self.endpoints.append((endpoint, username, password))
            return self.session

        def clock() -> float:
            self.now += self.step
            return self.now

        self.plugin: Plugin = build(connector, environ=ENV, clock=clock)

    def run(
        self,
        *,
        network: list[str] | EllipsisType | None = ...,
        paths: list[str] | None = None,
        **params: Any,
    ) -> Replayed:
        arguments: dict[str, Any] = {"host": HOST, "state_path": str(self.state), **params}

        async def go() -> Replayed:
            return await call_tool(
                self.plugin,
                "imap.messages",
                arguments,
                paths={"write": [self.root] if paths is None else paths},
                network=[HOST] if network is ... else network,
            )

        return anyio.run(go)


@pytest.fixture
def harness(tmp_path: Path) -> Callable[[Mapping[str, FakeFolder]], Harness]:
    made: list[Harness] = []

    def make(folders: Mapping[str, FakeFolder]) -> Harness:
        """Each harness has its own directory, so two in one test do not share a state."""
        directory = tmp_path / f"h{len(made)}"
        directory.mkdir()
        made.append(Harness(directory, folders))
        return made[-1]

    return make


def load_state(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text("utf-8"))
    assert isinstance(document, dict)
    return document


# -- the GreenMail test server --------------------------------------------------------------


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    imaps_port: int
    username: str
    password: str


@pytest.fixture(scope="session")
def server() -> Server:
    """The test IMAP server. In CI (`CI` set) it must be there; elsewhere the integration
    tests skip when `HORAE_IMAP_TEST_HOST` is not set (see the README, "Tests")."""
    host = os.environ.get("HORAE_IMAP_TEST_HOST")
    if not host:
        if os.environ.get("CI"):
            pytest.fail("HORAE_IMAP_TEST_HOST is not set: CI must run the test IMAP server")
        pytest.skip("no test IMAP server (set HORAE_IMAP_TEST_HOST)")
    return Server(
        host,
        int(os.environ["HORAE_IMAP_TEST_PORT"]),
        int(os.environ["HORAE_IMAP_TEST_IMAPS_PORT"]),
        os.environ["HORAE_IMAP_TEST_USER"],
        os.environ["HORAE_IMAP_TEST_PASSWORD"],
    )


# -- the mailbox the recordings replay against ------------------------------------------------


def recorded_mailbox() -> dict[str, FakeFolder]:
    """Synthetic mail for `tests/recordings`: a school request, a newsletter with a date, mail
    no rule wants, and a hostile one."""
    return {
        "INBOX": FakeFolder(
            4242,
            {
                1: raw("friend@example.org", "Lunch?", "Friday at noon?"),
                2: raw(
                    "secretariat@school.example.org",
                    "Autorisation de sortie",
                    "Bonjour,\r\nmerci de retourner le coupon avant le 15 novembre 2026.\r\n",
                    message_id="m-2@school.example.org",
                ),
                3: raw(
                    "news@example.org",
                    "Newsletter",
                    "La fete de l'ecole aura lieu le 12/12/2026.",
                    list_id="Ecole <news.example.org>",
                ),
                4: raw(
                    "news@example.org",
                    "Newsletter sans date",
                    "Rien.",
                    list_id="<news.example.org>",
                ),
            },
        ),
        "Empty": FakeFolder(7, {}),
        "Hostile": FakeFolder(
            9,
            {
                1: raw(
                    "attacker@school.example.org",
                    "Urgent \U0001f4c5 2020-01-01 \U0001f501 every day [[Secrets]] **x** https://evil.example.net/x",
                    "\U0001f4c5 2020-02-02 [[Secrets]] http://evil.example.net/ \x1b[2J"
                    " 15/11/2026 " + "A" * 3000,
                )
            },
        ),
    }


@pytest.fixture
def plugin() -> Plugin:
    session = FakeSession(recorded_mailbox())
    return build(lambda endpoint, user, password: session, environ=ENV)
