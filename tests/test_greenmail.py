# SPDX-License-Identifier: Apache-2.0
"""The plugin against a real IMAP server (GreenMail, a service container in CI).

The account and every message are synthetic. Each test uses its own folder. The flag tests
seed mail with the server's own client, then run the plugin and prove no flag changed."""

import imaplib
import json
import uuid
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import Server
from horae_imap import build
from horae_imap.session import folder_name

SCHOOL = {"name": "school", "kind": "request", "from": ["*@school.example.org"]}
NEWS = {"name": "news", "kind": "date", "list_id": ["news.example.org"]}


def message(
    subject: str, body: str, sender: str = "teacher@school.example.org", **headers: str
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "tester@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 02 Nov 2026 10:00:00 +0000"
    msg["Message-ID"] = f"<{uuid.uuid4()}@example.org>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg.as_bytes()


class Admin:
    """The test's own client: seeds mail and reads flags (never the plugin's code)."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.conn = imaplib.IMAP4(server.host, server.port, timeout=15)
        self.conn.login(server.username, server.password)

    def create(self) -> str:
        name = f"t-{uuid.uuid4().hex[:12]}"
        assert self.conn.create(name)[0] == "OK"
        return name

    def add(self, folder: str, raw: bytes, flags: str | None = None) -> None:
        assert self.conn.append(folder, flags, None, raw)[0] == "OK"

    def flags(self, folder: str) -> dict[int, frozenset[str]]:
        """Every message's flags, read through EXAMINE so the reading changes nothing.
        `\\Recent` is the server's own per-session marker, not a flag."""
        assert self.conn.select(f'"{folder}"', readonly=True)[0] == "OK"
        status, data = self.conn.uid("FETCH", "1:*", "(FLAGS)")
        assert status == "OK"
        found: dict[int, frozenset[str]] = {}
        for item in data:
            assert isinstance(item, bytes)
            text = item.decode()
            uid = int(text.split("UID ")[1].split()[0].rstrip(")"))
            raw_flags = text.split("FLAGS (")[1].split(")")[0].split()
            found[uid] = frozenset(f for f in raw_flags if f != "\\Recent")
        return found

    def unseen(self, folder: str) -> int:
        self.conn.select(f'"{folder}"', readonly=True)
        status, data = self.conn.uid("SEARCH", "UNSEEN")
        assert status == "OK"
        return len(data[0].split()) if data and data[0] else 0

    def close(self) -> None:
        self.conn.logout()


@pytest.fixture
def admin(server: Server) -> Iterator[Admin]:
    client = Admin(server)
    yield client
    client.close()


class Runner:
    def __init__(self, server: Server, tmp_path: Path, plugin: Plugin | None = None) -> None:
        self.server = server
        self.state = tmp_path / "state.json"
        self.tmp = tmp_path
        self.plugin = plugin or build(
            environ={"IMAP_USERNAME": server.username, "IMAP_PASSWORD": server.password}
        )

    def run(self, **params: Any) -> Replayed:
        arguments: dict[str, Any] = {
            "host": self.server.host,
            "port": self.server.port,
            "security": "plain",
            "allow_plaintext": True,
            "allow_loopback": True,
            "state_path": str(self.state),
            "timeout_s": 10,
            **params,
        }

        async def go() -> Replayed:
            return await call_tool(
                self.plugin,
                "imap.messages",
                arguments,
                paths={"write": [str(self.tmp)]},
                network=None,
            )

        return anyio.run(go)


@pytest.fixture
def runner(server: Server, tmp_path: Path) -> Runner:
    return Runner(server, tmp_path)


def test_new_mail_is_read_once(admin: Admin, runner: Runner) -> None:
    folder = admin.create()
    admin.add(folder, message("Old form", "Please answer by 01/10/2026."))
    first = runner.run(folders=[folder], rules=[SCHOOL, NEWS])
    assert first.error is None, first.error
    assert first.data["items"] == []  # the first run only baselines
    assert first.data["folders"][0]["baselined"] is True

    admin.add(folder, message("Autorisation de sortie", "Merci de répondre avant le 15/11/2026."))
    admin.add(folder, message("Lunch", "Nope", sender="bob@example.org"))
    admin.add(
        folder,
        message(
            "Newsletter",
            "Fête de l'école le 12 décembre",
            sender="n@example.org",
            List_Id="School <news.example.org>",
        ),
    )
    second = runner.run(folders=[folder], rules=[SCHOOL, NEWS], emit_tasks=True)
    assert second.error is None, second.error
    items = second.data["items"]
    assert [(i["kind"], i["rule"], i["subject"]) for i in items] == [
        ("request", "school", "Autorisation de sortie"),
        ("date", "news", "Newsletter"),
    ]
    assert items[0]["due"] == "2026-11-15"
    assert items[1]["due"] == "2026-12-12"
    assert [t["type"] for t in second.intents] == ["task.create", "task.create"]
    assert second.intents[0]["fingerprint"].startswith(f"imap:request:mail:{folder}:")

    third = runner.run(folders=[folder], rules=[SCHOOL, NEWS])
    assert third.data["items"] == []
    assert third.data["folders"][0]["fetched"] == 0

    state = json.loads(runner.state.read_text("utf-8"))
    assert state["accounts"]["mail"][folder]["last_uid"] == 4


def test_a_run_never_changes_any_flag(admin: Admin, runner: Runner) -> None:
    """The acceptance criterion: mail is never marked read. Unseen, seen, flagged and answered
    messages, read by the plugin through several runs, keep exactly the flags they had."""
    folder = admin.create()
    admin.add(folder, message("Unseen one", "Please reply by 15/11/2026."))
    admin.add(folder, message("Seen one", "Please reply by 16/11/2026."), r"(\Seen)")
    admin.add(folder, message("Flagged one", "Please reply by 17/11/2026."), r"(\Flagged)")
    admin.add(folder, message("Answered one", "Please reply by 18/11/2026."), r"(\Answered \Seen)")
    admin.add(folder, message("Draft one", "Please reply by 19/11/2026."), r"(\Draft)")
    before = admin.flags(folder)
    assert before[1] == frozenset()
    assert admin.unseen(folder) == 3

    read = runner.run(folders=[folder], rules=[SCHOOL], baseline_messages=10)
    assert read.error is None, read.error
    assert len(read.data["items"]) == 5  # every message really was fetched and judged
    assert [i["due"] for i in read.data["items"]][:2] == ["2026-11-15", "2026-11-16"]
    admin.add(folder, message("Unseen two", "Please reply by 20/11/2026."))
    before_two = admin.flags(folder)
    assert runner.run(folders=[folder], rules=[SCHOOL]).error is None
    assert runner.run(folders=[folder], rules=[SCHOOL], update_state=False).error is None
    # a re-baseline reads the newest mail again
    state = json.loads(runner.state.read_text("utf-8"))
    state["accounts"]["mail"][folder]["uidvalidity"] += 1
    runner.state.write_text(json.dumps(state), "utf-8")
    assert runner.run(folders=[folder], rules=[SCHOOL], baseline_messages=10).error is None

    after = admin.flags(folder)
    assert after == before_two
    assert {uid: flags for uid, flags in after.items() if uid in before} == before
    assert admin.unseen(folder) == 4  # the unseen ones are still unseen


def test_only_read_only_commands_reach_the_server(
    admin: Admin, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = admin.create()
    admin.add(folder, message("A form", "Reply by 15/11/2026."))
    sent: list[tuple[str, tuple[Any, ...]]] = []
    original: Callable[..., Any] = imaplib.IMAP4._command  # type: ignore[attr-defined]

    def recording(self: imaplib.IMAP4, name: str, *args: Any) -> Any:
        sent.append((name, args))
        return original(self, name, *args)

    monkeypatch.setattr(imaplib.IMAP4, "_command", recording)
    runner.run(folders=[folder], rules=[SCHOOL], baseline_messages=5)
    runner.run(folders=[folder], rules=[SCHOOL])
    monkeypatch.undo()

    names = [name for name, _ in sent]
    assert set(names) <= {"CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT"}
    assert "SELECT" not in names
    assert names.count("EXAMINE") == 2
    fetches = [str(args[-1]) for name, args in sent if name == "UID" and args[0] == "FETCH"]
    assert fetches, "the messages were fetched"
    for items in fetches:
        assert "BODY.PEEK[" in items
        assert "BODY[" not in items.replace("BODY.PEEK[", "")
        assert "RFC822" not in items
    searches = [args for name, args in sent if name == "UID" and args[0] == "SEARCH"]
    assert searches


def test_the_server_itself_refuses_changes_in_an_examined_folder(
    admin: Admin, server: Server
) -> None:
    """Defence in depth: even a bug could not store a flag, because the folder is EXAMINEd."""
    folder = admin.create()
    admin.add(folder, message("x", "y"))
    conn = imaplib.IMAP4(server.host, server.port, timeout=15)
    conn.login(server.username, server.password)
    assert conn.select(f'"{folder}"', readonly=True)[0] == "OK"
    assert conn.uid("STORE", "1", "+FLAGS", r"(\Seen)")[0] == "NO"
    conn.logout()
    assert admin.flags(folder)[1] == frozenset()


def test_a_changed_uidvalidity_rebaselines_against_a_real_server(
    admin: Admin, runner: Runner
) -> None:
    folder = admin.create()
    for n in range(4):
        admin.add(folder, message(f"Form {n}", "Reply by 15/11/2026."))
    runner.run(folders=[folder], rules=[SCHOOL])
    state = json.loads(runner.state.read_text("utf-8"))
    state["accounts"]["mail"][folder]["uidvalidity"] += 7  # as if the server renumbered
    runner.state.write_text(json.dumps(state), "utf-8")
    result = runner.run(folders=[folder], rules=[SCHOOL], baseline_messages=2)
    entry = result.data["folders"][0]
    assert (entry["uidvalidity_changed"], entry["baselined"]) == (True, True)
    assert [i["uid"] for i in result.data["items"]] == [3, 4]  # the newest two, no flood


def test_a_cap_over_a_real_folder(admin: Admin, runner: Runner) -> None:
    folder = admin.create()
    admin.add(folder, message("seed", "x"))
    runner.run(folders=[folder], rules=[SCHOOL])
    for n in range(7):
        admin.add(folder, message(f"Form {n}", "Reply by 15/11/2026."))
    first = runner.run(folders=[folder], rules=[SCHOOL], max_messages=3)
    assert [i["uid"] for i in first.data["items"]] == [2, 3, 4]
    assert first.data["truncated"] is True
    rest = runner.run(folders=[folder], rules=[SCHOOL], max_messages=10)
    assert [i["uid"] for i in rest.data["items"]] == [5, 6, 7, 8]
    assert rest.data["truncated"] is False


def test_the_body_is_capped_on_the_wire(admin: Admin, runner: Runner) -> None:
    folder = admin.create()
    admin.add(folder, message("seed", "x"))
    runner.run(folders=[folder], rules=[SCHOOL])
    admin.add(folder, message("Big", ("padding " * 400) + "Reply by 15/11/2026."))
    result = runner.run(folders=[folder], rules=[SCHOOL], max_body_kib=1, snippet_chars=40)
    item = result.data["items"][0]
    assert item["dates"] == []  # the date sits after the first KiB: never fetched
    assert len(item["snippet"]) <= 48


def test_a_missing_folder_fails_and_writes_nothing(admin: Admin, runner: Runner) -> None:
    result = runner.run(folders=["no-such-folder"], rules=[SCHOOL])
    assert result.error is not None
    assert "folder_unavailable" in result.error
    assert not runner.state.exists()


def test_folder_names_with_spaces_and_accents(admin: Admin, runner: Runner) -> None:
    name = f"Écoles {uuid.uuid4().hex[:6]}"
    quoted = folder_name(name)  # modified UTF-7, the same encoding the plugin sends
    assert admin.conn.create(quoted)[0] == "OK"
    admin.conn.append(quoted, None, None, message("seed", "x"))
    runner.run(folders=[name], rules=[SCHOOL])
    admin.conn.append(quoted, None, None, message("Form", "Reply by 15/11/2026."))
    result = runner.run(folders=[name], rules=[SCHOOL])
    assert result.error is None, result.error
    assert [(i["folder"], i["uid"]) for i in result.data["items"]] == [(name, 2)]


def test_wrong_credentials_are_a_fixed_error_without_the_password(
    server: Server, tmp_path: Path
) -> None:
    secret = "not-the-password-0123"  # noqa: S105
    plugin = build(environ={"IMAP_USERNAME": server.username, "IMAP_PASSWORD": secret})
    result = Runner(server, tmp_path, plugin).run(rules=[SCHOOL])
    assert result.error is not None
    assert "login_failed" in result.error
    assert secret not in result.error
    assert server.password not in result.error


def test_the_certificate_is_verified_on_implicit_tls(admin: Admin, runner: Runner) -> None:
    """GreenMail's IMAPS certificate is self-signed: the default verification must refuse it."""
    result = runner.run(
        security="ssl", port=runner.server.imaps_port, allow_plaintext=False, rules=[SCHOOL]
    )
    assert result.error is not None
    assert result.error.startswith("tls_failed") or "tls_failed" in result.error
    assert not runner.state.exists()


def test_starttls_is_refused_when_the_server_has_none(runner: Runner) -> None:
    result = runner.run(security="starttls", rules=[SCHOOL])
    assert result.error is not None
    assert "starttls_unsupported" in result.error


def test_an_unreachable_server_is_a_fixed_error(runner: Runner) -> None:
    result = runner.run(port=9, rules=[SCHOOL], timeout_s=3)
    assert result.error is not None
    assert "connect_failed" in result.error or "timeout" in result.error
