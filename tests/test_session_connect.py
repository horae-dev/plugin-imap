# SPDX-License-Identifier: Apache-2.0
"""Connection set-up and failure mapping with a scripted imaplib, no server needed: which
class is opened for which security, that TLS is verified, and that every failure becomes a
fixed error that never holds the password."""

import imaplib
import ssl
from typing import Any

import pytest
from horae_sdk import PluginError

from horae_imap import session
from horae_imap.session import Endpoint, ImapSession, Selected

SECRET = "scripted-password-0123"  # noqa: S105
SERVER = "mail.example.org"


class Scripted:
    """Stands for `imaplib.IMAP4` and `IMAP4_SSL`."""

    instances: list["Scripted"] = []  # noqa: RUF012
    boom: BaseException | None = None
    caps: tuple[str, ...] = ("IMAP4REV1", "STARTTLS")
    login_error: BaseException | None = None
    answers: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self, host: str, port: int = 143, *args: Any, **kwargs: Any) -> None:
        if Scripted.boom is not None:
            raise Scripted.boom
        self.args = (host, port, args, kwargs)
        self.tls: ssl.SSLContext | None = None
        self.capabilities = Scripted.caps
        self.is_readonly = True
        self.untagged_responses: dict[str, list[bytes]] = {}
        self.calls: list[str] = []
        self.shutdown_called = False
        Scripted.instances.append(self)

    def starttls(self, ssl_context: ssl.SSLContext) -> tuple[str, list[bytes]]:
        self.calls.append("starttls")
        self.tls = ssl_context
        return "OK", [b""]

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append("login")
        if Scripted.login_error is not None:
            raise Scripted.login_error
        return "OK", [b""]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(f"select {mailbox} {readonly}")
        self.untagged_responses = Scripted.answers.get("untagged", {})
        answer: tuple[str, list[bytes]] = Scripted.answers.get("select", ("OK", [b"3"]))
        return answer

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(f"uid {command}")
        found: tuple[str, list[Any]] = Scripted.answers.get(command, ("OK", [None]))
        return found

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append("logout")
        if Scripted.answers.get("logout_error"):
            raise OSError("gone")
        return "BYE", [b""]

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def scripted(monkeypatch: pytest.MonkeyPatch) -> type[Scripted]:
    Scripted.instances = []
    Scripted.boom = None
    Scripted.caps = ("IMAP4REV1", "STARTTLS")
    Scripted.login_error = None
    Scripted.answers = {}
    monkeypatch.setattr(
        imaplib, "IMAP4", type("IMAP4", (Scripted,), {"error": imaplib.IMAP4.error})
    )
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Scripted)
    return Scripted


def connect(security: str = "ssl", port: int = 993) -> ImapSession:
    return ImapSession(Endpoint(SERVER, port, security, 7.0), "user", SECRET)  # type: ignore[arg-type]


def fails(security: str = "ssl") -> str:
    with pytest.raises(PluginError) as caught:
        connect(security)
    return str(caught.value)


def test_implicit_tls_is_the_default_and_verified(scripted: type[Scripted]) -> None:
    connect()
    host, port, _, kwargs = scripted.instances[0].args
    assert (host, port) == (SERVER, 993)
    context = kwargs["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert kwargs["timeout"] == 7.0
    assert scripted.instances[0].calls == ["login"]


def test_starttls_upgrades_before_the_login_with_a_verifying_context(
    scripted: type[Scripted],
) -> None:
    connect("starttls", 143)
    conn = scripted.instances[0]
    assert conn.calls == ["starttls", "login"]
    assert conn.tls is not None
    assert conn.tls.verify_mode == ssl.CERT_REQUIRED
    assert conn.args[2] == (7.0,)  # the timeout


def test_starttls_never_falls_back_to_plain_text(scripted: type[Scripted]) -> None:
    scripted.caps = ("IMAP4REV1",)
    assert "starttls_unsupported" in fails("starttls")
    assert scripted.instances[0].calls == []  # no login was sent
    assert scripted.instances[0].shutdown_called


def test_plain_sends_nothing_but_the_login(scripted: type[Scripted]) -> None:
    connect("plain", 143)
    assert scripted.instances[0].calls == ["login"]


def test_a_server_that_disables_login_gets_no_password(scripted: type[Scripted]) -> None:
    scripted.caps = ("IMAP4REV1", "LOGINDISABLED")
    assert "login_disabled" in fails("plain")
    assert "login" not in scripted.instances[0].calls
    assert scripted.instances[0].shutdown_called


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ssl.SSLCertVerificationError("bad cert for mail.example.org"), "tls_failed"),
        (ssl.SSLError("handshake"), "tls_failed"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError("refused"), "connect_failed"),
        (OSError("unreachable"), "connect_failed"),
        (EOFError(), "connect_failed"),
        (imaplib.IMAP4.error("bad greeting"), "connect_failed"),
    ],
)
def test_connection_failures_are_fixed_codes(
    scripted: type[Scripted], error: BaseException, code: str
) -> None:
    scripted.boom = error
    message = fails()
    assert message.startswith(code)
    assert SECRET not in message
    assert not str(error) or str(error) not in message  # nothing the network said


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (imaplib.IMAP4.error("LOGIN failed: [AUTHENTICATIONFAILED] secret hint"), "login_failed"),
        (UnicodeEncodeError("ascii", "é", 0, 1, "ordinal"), "login_failed"),
        (ssl.SSLError("mid-login"), "tls_failed"),
        (TimeoutError(), "timeout"),
        (ConnectionResetError(), "connect_failed"),
    ],
)
def test_login_failures_are_fixed_codes_and_close_the_connection(
    scripted: type[Scripted], error: BaseException, code: str
) -> None:
    scripted.login_error = error
    message = fails()
    assert message.startswith(code)
    assert SECRET not in message
    assert "hint" not in message
    assert scripted.instances[0].shutdown_called


def test_examine_reads_the_selection_and_demands_read_only(scripted: type[Scripted]) -> None:
    scripted.answers["untagged"] = {
        "UIDVALIDITY": [b"77"],
        "UIDNEXT": [b"12"],
        "EXISTS": [b"11"],
    }
    scripted.answers["select"] = ("OK", [b"11"])
    opened = connect()
    assert opened.examine("INBOX") == Selected(77, 12, 11)
    assert scripted.instances[0].calls[-1] == 'select "INBOX" True'

    scripted.answers["untagged"] = {"UIDVALIDITY": [b"77"]}
    scripted.answers["select"] = ("OK", [None])
    assert opened.examine("INBOX") == Selected(77, None, 0)

    scripted.answers["untagged"] = {"EXISTS": [b"4"]}
    with pytest.raises(PluginError, match="UIDVALIDITY"):
        opened.examine("INBOX")

    scripted.answers["select"] = ("NO", [b"nope"])
    with pytest.raises(PluginError, match="folder_unavailable"):
        opened.examine("Missing")

    scripted.answers["select"] = ("OK", [b"1"])
    scripted.answers["untagged"] = {"UIDVALIDITY": [b"1"]}
    scripted.instances[0].is_readonly = False  # a server that gave write access anyway
    with pytest.raises(PluginError, match="read-only"):
        opened.examine("INBOX")


def test_search_and_fetch_parsing_is_defensive(scripted: type[Scripted]) -> None:
    opened = connect()
    scripted.answers["SEARCH"] = ("OK", [b"3 5 x 99 1000000 4"])
    assert opened.uids(2, 10) == [3, 4, 5]  # only what was asked for
    scripted.answers["SEARCH"] = ("OK", [None])
    assert opened.uids(1, 5) == []
    scripted.answers["SEARCH"] = ("NO", [b""])
    assert opened.uids(1, 5) == []
    scripted.answers["FETCH"] = (
        "OK",
        [b"12 (UID 41)", b"13 (UID 42 FLAGS ())", (b"x UID 7", b"y")],
    )
    assert opened.highest_uid() == 42
    scripted.answers["FETCH"] = ("NO", [])
    assert opened.highest_uid() == 0
    scripted.answers["FETCH"] = (
        "OK",
        [
            (b"1 (UID 4 BODY[HEADER.FIELDS (FROM)] {5}", b"From: a@example.org\r\n" + b"z" * 99),
            (b" BODY[TEXT]<0> {3}", b"abcdefgh"),
            b")",
        ],
    )
    headers, text = opened.fetch(4, body_bytes=4) or (b"", b"")
    assert headers.startswith(b"From: a@example.org")
    assert text == b"abcd"  # cut to the cap even if the server sent more
    scripted.answers["FETCH"] = ("OK", [b"1 (UID 4)", b")"])
    assert opened.fetch(4, body_bytes=4) is None
    scripted.answers["FETCH"] = ("NO", [])
    assert opened.fetch(4, body_bytes=4) is None


@pytest.mark.parametrize(
    "error", [TimeoutError(), OSError("x"), EOFError(), imaplib.IMAP4.error("x")]
)
def test_a_request_that_dies_is_a_fixed_error(
    scripted: type[Scripted], error: BaseException
) -> None:
    opened = connect()

    def broken(*args: Any) -> Any:
        raise error

    scripted.instances[0].uid = broken  # type: ignore[method-assign,assignment]
    with pytest.raises(PluginError) as caught:
        opened.uids(1, 5)
    assert str(caught.value).split(":")[0] in {"timeout", "protocol_error"}
    assert "x" not in str(caught.value).split(": ", 1)[1].replace("the server dropped", "")


def test_examine_failures_are_fixed(scripted: type[Scripted]) -> None:
    opened = connect()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("socket detail")

    scripted.instances[0].select = broken  # type: ignore[method-assign]
    with pytest.raises(PluginError, match="folder_unavailable") as caught:
        opened.examine("INBOX")
    assert "socket detail" not in str(caught.value)


def test_logout_survives_a_dead_connection(scripted: type[Scripted]) -> None:
    opened = connect()
    opened.logout()
    assert scripted.instances[0].calls[-1] == "logout"
    scripted.answers["logout_error"] = True
    opened.logout()
    assert scripted.instances[0].shutdown_called


def test_connect_is_the_default_connector(scripted: type[Scripted]) -> None:
    assert isinstance(session.connect(Endpoint(SERVER, 993, "ssl", 5.0), "u", SECRET), ImapSession)
