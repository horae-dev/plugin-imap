# SPDX-License-Identifier: Apache-2.0
"""The IMAP session, read-only by construction.

Every command the plugin can send goes through `Session._run`, which holds the allow-list:
`CAPABILITY`, `STARTTLS`, `LOGIN`, `EXAMINE`, `UID SEARCH`, `UID FETCH` and `LOGOUT`.

- A folder is opened with **`EXAMINE`** (imaplib's `select(readonly=True)`), never `SELECT`;
  the server then refuses any change, and the session checks the mailbox came back read-only.
- A message is fetched with **`BODY.PEEK[...]`** only, which never sets `\\Seen`; `BODY[...]`,
  `BINARY[...]` and `RFC822` are refused before they are sent.
- There is no `STORE`, `EXPUNGE`, `COPY`, `MOVE`, `APPEND`, `DELETE`, `CLOSE` or `SELECT` in the
  allow-list or anywhere in the package (a test searches the source for them, and another
  records every command sent to a real server).

TLS: implicit TLS (IMAPS) by default, or `STARTTLS`; both verify the certificate and the host
name with the system trust store (`ssl.create_default_context()`, never relaxed). Plain text is
only opened when the caller asks for it, and then it is done before any credential is sent only
if the server does not advertise `LOGINDISABLED`.
"""

import contextlib
import imaplib
import re
import ssl
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from horae_sdk import PluginError

from horae_imap.message import HEADER_FIELDS, MAX_HEADER_BYTES

Security = Literal["ssl", "starttls", "plain"]
ALLOWED = frozenset(
    {"CAPABILITY", "STARTTLS", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
)
"""Every command the session may send."""
UNSAFE_FETCH = r"BODY\[|BINARY\[|RFC822"
"""Fetch items that mark a message read (the server sets `\\Seen` as a side effect)."""
MAX_LINE = 4_000_000
"""imaplib's cap on one response line (its default is 1 MB): a search window stays far below."""
_HIGHEST = re.compile(rb"\bUID (\d{1,10})\b")


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    security: Security
    timeout: float


@dataclass(frozen=True)
class Selected:
    """A folder opened read-only."""

    uidvalidity: int
    uidnext: int | None
    exists: int


class Session(Protocol):
    """What the plugin needs of a mailbox; the tests give it fakes."""

    def examine(self, folder: str) -> Selected: ...

    def uids(self, low: int, high: int) -> list[int]:
        """The UIDs in `low` to `high` that exist, ascending."""
        ...

    def highest_uid(self) -> int:
        """The highest UID of the opened folder, 0 when it is empty."""
        ...

    def fetch(self, uid: int, *, body_bytes: int) -> tuple[bytes, bytes] | None:
        """The header fields and the first `body_bytes` of the text of one message, without
        touching its flags; `None` when it is gone."""
        ...

    def logout(self) -> None: ...


Connector = Callable[["Endpoint", str, str], Session]
"""`(endpoint, username, password) -> Session`; replaced in tests."""


def folder_name(name: str) -> str:
    """`name` as an IMAP quoted mailbox name: modified UTF-7 (RFC 3501 section 5.1.3) for
    non-ASCII, `\\` and `"` escaped. Control characters are refused."""
    if not name or len(name) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise PluginError("config_invalid: a folder name is 1 to 200 printable characters")
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            encoded = b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in name:
        if " " <= ch <= "~":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return '"' + "".join(out).replace("\\", "\\\\").replace('"', '\\"') + '"'


def tls_context() -> ssl.SSLContext:
    """The default client context: certificate and host name verified, TLS 1.2 or later."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class ImapSession:
    """A logged-in, read-only session on one server."""

    def __init__(self, endpoint: Endpoint, username: str, password: str) -> None:
        imaplib._MAXLINE = MAX_LINE  # type: ignore[attr-defined]
        self._conn: imaplib.IMAP4
        self._open(endpoint)
        try:
            self._login(endpoint, username, password)
        except BaseException:
            self._drop()
            raise

    # -- connection ---------------------------------------------------------------------

    def _open(self, endpoint: Endpoint) -> None:
        try:
            if endpoint.security == "ssl":
                self._conn = imaplib.IMAP4_SSL(
                    endpoint.host,
                    endpoint.port,
                    ssl_context=tls_context(),
                    timeout=endpoint.timeout,
                )
            else:
                self._conn = imaplib.IMAP4(endpoint.host, endpoint.port, endpoint.timeout)
                if endpoint.security == "starttls":
                    self._start_tls()
        except ssl.SSLCertVerificationError:
            raise PluginError("tls_failed: the server certificate was not accepted") from None
        except ssl.SSLError:
            raise PluginError("tls_failed: the TLS handshake failed") from None
        except TimeoutError:
            raise PluginError("timeout: the server did not answer in time") from None
        except (OSError, imaplib.IMAP4.error, EOFError):
            raise PluginError("connect_failed: cannot reach the server") from None

    def _start_tls(self) -> None:
        if "STARTTLS" not in self._capabilities():
            self._drop()
            raise PluginError("starttls_unsupported: the server does not offer STARTTLS")
        self._run("STARTTLS", ssl_context=tls_context())

    def _capabilities(self) -> tuple[str, ...]:
        return tuple(str(c).upper() for c in self._conn.capabilities)

    def _login(self, endpoint: Endpoint, username: str, password: str) -> None:
        if "LOGINDISABLED" in self._capabilities():
            raise PluginError("login_disabled: the server refuses LOGIN on this connection")
        try:
            self._run("LOGIN", username, password)
        except imaplib.IMAP4.error:
            raise PluginError("login_failed: the server refused the credentials") from None
        except UnicodeError:
            raise PluginError("login_failed: the credentials are not ASCII") from None
        except ssl.SSLError:
            raise PluginError("tls_failed: the TLS connection failed") from None
        except TimeoutError:
            raise PluginError("timeout: the server did not answer in time") from None
        except (OSError, EOFError):
            raise PluginError("connect_failed: the connection dropped") from None

    def _drop(self) -> None:
        with contextlib.suppress(Exception):  # closing a connection that may be broken
            self._conn.shutdown()

    # -- the only way a command leaves ----------------------------------------------------

    def _run(self, command: str, *args: Any, **kwargs: Any) -> Any:
        """Send one allowed command; anything else is a bug and raises before sending."""
        if command == "UID":
            command = f"UID {args[0]}"
        if command not in ALLOWED:
            raise AssertionError(f"command {command} is not allowed")
        if command == "UID FETCH" and re.search(UNSAFE_FETCH, str(args[-1]).upper()):
            # `BODY[...]`, `BINARY[...]` and `RFC822*` set \Seen; their `.PEEK` forms do not
            raise AssertionError("a fetch must use BODY.PEEK only")
        if command.startswith("UID "):
            return self._conn.uid(*args)
        if command == "EXAMINE":
            return self._conn.select(args[0], readonly=True)
        if command == "CAPABILITY":
            return self._conn.capability()
        if command == "STARTTLS":
            return self._conn.starttls(**kwargs)
        if command == "LOGIN":
            return self._conn.login(*args)
        return self._conn.logout()

    # -- the Session protocol -------------------------------------------------------------

    def examine(self, folder: str) -> Selected:
        try:
            status, data = self._run("EXAMINE", folder_name(folder))
        except (OSError, imaplib.IMAP4.error, EOFError):
            raise PluginError(f"folder_unavailable: cannot open {folder!r}") from None
        if status != "OK" or not self._conn.is_readonly:
            raise PluginError(f"folder_unavailable: cannot open {folder!r} read-only")
        untagged = self._conn.untagged_responses

        def number(key: str) -> int | None:
            values = untagged.get(key) or []
            last = values[-1] if values else None
            return int(last) if isinstance(last, bytes) and last.isdigit() else None

        validity = number("UIDVALIDITY")
        if validity is None:
            raise PluginError("protocol_error: the server gave no UIDVALIDITY")
        first = data[0] if data else None
        exists = int(first) if isinstance(first, bytes) and first.isdigit() else number("EXISTS")
        return Selected(validity, number("UIDNEXT"), exists or 0)

    def uids(self, low: int, high: int) -> list[int]:
        status, data = self._guarded("SEARCH", "UID", f"{low}:{high}")
        if status != "OK" or not data or not data[0]:
            return []
        found = {int(n) for n in data[0].split() if n.isdigit()}
        return sorted(n for n in found if low <= n <= high)

    def highest_uid(self) -> int:
        status, data = self._guarded("FETCH", "*", "(UID)")
        if status != "OK":
            return 0
        highest = 0
        for item in data:
            raw = item[0] if isinstance(item, tuple) else item
            if isinstance(raw, bytes):
                highest = max([highest, *(int(n) for n in _HIGHEST.findall(raw))])
        return highest

    def fetch(self, uid: int, *, body_bytes: int) -> tuple[bytes, bytes] | None:
        fields = " ".join(HEADER_FIELDS)
        items = (
            f"(UID BODY.PEEK[HEADER.FIELDS ({fields})]<0.{MAX_HEADER_BYTES}> "
            f"BODY.PEEK[TEXT]<0.{body_bytes}>)"
        )
        status, data = self._guarded("FETCH", str(uid), items)
        if status != "OK":
            return None
        headers: bytes | None = None
        text = b""
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[1], bytes):
                continue
            label = item[0].upper() if isinstance(item[0], bytes) else b""
            if b"HEADER.FIELDS" in label:
                headers = item[1][:MAX_HEADER_BYTES]
            elif b"TEXT" in label:
                text = item[1][:body_bytes]
        return None if headers is None else (headers, text)

    def _guarded(self, *args: Any) -> tuple[str, list[Any]]:
        try:
            status, data = self._run("UID", *args)
        except TimeoutError:
            raise PluginError("timeout: the server did not answer in time") from None
        except (OSError, imaplib.IMAP4.error, EOFError, ValueError):
            raise PluginError("protocol_error: the server dropped or refused a request") from None
        return str(status), list(data or [])

    def logout(self) -> None:
        try:
            self._run("LOGOUT")
        except (OSError, imaplib.IMAP4.error, EOFError):
            self._drop()


def connect(endpoint: Endpoint, username: str, password: str) -> Session:
    """The default connector: a logged-in `ImapSession`."""
    return ImapSession(endpoint, username, password)
