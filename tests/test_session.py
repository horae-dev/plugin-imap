# SPDX-License-Identifier: Apache-2.0
"""The session's own rules, without a server: what may be sent, folder names, TLS settings.
`test_greenmail.py` proves the same against a real server."""

import ast
import re
import ssl
from pathlib import Path
from typing import Any

import pytest

from horae_imap import session
from horae_imap.session import ALLOWED, ImapSession, folder_name, tls_context

SOURCE = Path(session.__file__).parent
GUARD = session.UNSAFE_FETCH


class Recorder:
    """Stands where imaplib's connection is: records the calls the session makes."""

    is_readonly = True
    untagged_responses: dict[str, list[bytes]] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> tuple[str, list[bytes]]:
            self.calls.append((name, args, kwargs))
            return "OK", [b""]

        return record


def bare() -> tuple[ImapSession, Recorder]:
    made = object.__new__(ImapSession)
    recorder = Recorder()
    made._conn = recorder  # type: ignore[assignment]
    return made, recorder


def test_the_allow_list_is_exactly_the_read_only_commands() -> None:
    assert {
        "CAPABILITY",
        "STARTTLS",
        "LOGIN",
        "EXAMINE",
        "UID SEARCH",
        "UID FETCH",
        "LOGOUT",
    } == ALLOWED


@pytest.mark.parametrize(
    "command",
    [
        "STORE",
        "UID STORE",
        "EXPUNGE",
        "UID EXPUNGE",
        "COPY",
        "UID COPY",
        "UID MOVE",
        "MOVE",
        "SELECT",
        "APPEND",
        "DELETE",
        "CREATE",
        "CLOSE",
        "RENAME",
        "SETFLAGS",
    ],
)
def test_a_command_off_the_list_is_never_sent(command: str) -> None:
    made, recorder = bare()
    verb, _, rest = command.partition(" ")
    args = (rest or "1", "+FLAGS", r"(\Seen)") if verb == "UID" else ("INBOX",)
    with pytest.raises(AssertionError, match="not allowed"):
        made._run(verb, *args)
    assert recorder.calls == []


@pytest.mark.parametrize(
    "items",
    [
        "(BODY[])",
        "(BODY[TEXT])",
        "(body[text])",
        "(RFC822)",
        "(RFC822.TEXT)",
        "(UID BODY[HEADER])",
        "(BINARY[1])",
        "(UID BODY.PEEK[TEXT] BODY[])",
    ],
)
def test_a_fetch_without_peek_is_never_sent(items: str) -> None:
    made, recorder = bare()
    with pytest.raises(AssertionError, match="PEEK"):
        made._run("UID", "FETCH", "1", items)
    assert recorder.calls == []


def test_peek_fetches_and_examine_pass() -> None:
    made, recorder = bare()
    made._run("UID", "FETCH", "1", "(UID BODY.PEEK[TEXT]<0.100>)")
    made._run("UID", "FETCH", "*", "(UID)")  # no body item: nothing to mark read
    made._run("EXAMINE", '"INBOX"')
    assert recorder.calls == [
        ("uid", ("FETCH", "1", "(UID BODY.PEEK[TEXT]<0.100>)"), {}),
        ("uid", ("FETCH", "*", "(UID)"), {}),
        ("select", ('"INBOX"',), {"readonly": True}),  # EXAMINE, not SELECT
    ]


def test_the_package_has_no_way_to_change_a_mailbox() -> None:
    """A guard on the source itself (docstrings and comments aside): no write verb or un-peeked
    body in any string, and the only methods called on the connection are the read-only ones."""
    verbs = re.compile(r"\b(STORE|EXPUNGE|COPY|MOVE|APPEND|DELETE|SELECT|CLOSE|RFC822)\b|BODY\[")
    allowed_methods = {"uid", "select", "capability", "starttls", "login", "logout", "shutdown"}
    allowed_state = {"untagged_responses", "is_readonly", "capabilities"}
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and node.value != GUARD  # the session's own refusal pattern
            ):
                assert not verbs.search(node.value), f"{path.name}:{node.lineno}"
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_conn"
            ):
                assert node.attr in allowed_methods | allowed_state, (
                    f"{path.name}:{node.lineno}: {node.attr}"
                )
    # `select` is only ever EXAMINE
    calls = [
        node
        for node in ast.walk(ast.parse((SOURCE / "session.py").read_text("utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "select"
    ]
    assert len(calls) == 1
    assert [(k.arg, getattr(k.value, "value", None)) for k in calls[0].keywords] == [
        ("readonly", True)
    ]


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("INBOX", '"INBOX"'),
        ("Lists/School", '"Lists/School"'),
        ("My Folder", '"My Folder"'),
        ("R&D", '"R&-D"'),
        ("Envoyés", '"Envoy&AOk-s"'),
        ("日本語", '"&ZeVnLIqe-"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_folder_names(name: str, encoded: str) -> None:
    assert folder_name(name) == encoded


@pytest.mark.parametrize("name", ["", "a\r\nb LOGOUT", "x\x00", "a\x7fb", "x" * 201])
def test_hostile_folder_names_are_refused(name: str) -> None:
    from horae_sdk import PluginError

    with pytest.raises(PluginError, match="folder"):
        folder_name(name)


def test_tls_is_verified() -> None:
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
