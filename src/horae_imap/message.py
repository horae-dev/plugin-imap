# SPDX-License-Identifier: Apache-2.0
"""Turn the bytes fetched from the server into a `Message`: a few headers and a capped text.

Everything here is **untrusted**. Parsing never raises on a hostile message: a header or a
body that cannot be read is simply absent. Only the headers and the first bytes of the
`text/plain` part ever come in (the fetch is `BODY.PEEK` with a partial range), and the text
is cut again here.
"""

import email
import email.policy
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.message import Message as _Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import cast

MAX_HEADER_BYTES = 16 * 1024
"""Headers fetched per message (the requested header fields only)."""
MAX_FIELD = 998
"""Characters kept of a header value (RFC 5322's line limit)."""
HEADER_FIELDS = (
    "FROM",
    "SUBJECT",
    "DATE",
    "MESSAGE-ID",
    "LIST-ID",
    "CONTENT-TYPE",
    "CONTENT-TRANSFER-ENCODING",
    "MIME-VERSION",
)
"""The only headers the plugin asks for."""

_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[ \t\r\n\f\v]+")
_LIST_ID = re.compile(r"<([^<>\s]{1,255})>")


@dataclass(frozen=True)
class Message:
    uid: int
    sender: str
    """The bare address, lower case (the display name is never used: it is trivial to forge)."""
    subject: str
    list_id: str
    message_id: str
    day: date | None
    """The `Date` header's day, `None` when absent or unreadable."""
    text: str
    """The capped, cleaned start of the `text/plain` part; empty when there is none."""


def clean(value: str, limit: int) -> str:
    """One line, control, format, surrogate and private-use characters removed, cut to
    `limit` characters."""
    kept = "".join(
        " " if ch in "\r\n\t" else ch
        for ch in value
        if unicodedata.category(ch) not in ("Cf", "Cs", "Co", "Cn") or ch in "\r\n\t"
    )
    kept = _UNSAFE.sub("", kept)
    return _SPACES.sub(" ", kept).strip()[:limit]


def _header(msg: _Message, name: str) -> str:
    try:
        value = msg[name]
    except Exception:  # a header the parser cannot read: absent
        return ""
    return clean(str(value), MAX_FIELD) if value is not None else ""


def _day(value: str) -> date | None:
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _body_text(msg: _Message, limit: int) -> str:
    """The `text/plain` body, decoded leniently; the whole message when it is not multipart."""
    try:
        part = cast(EmailMessage, msg).get_body(preferencelist=("plain",))
    except Exception:
        return ""
    if part is None:
        return ""
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = raw[: limit * 4].decode(charset, "replace")
    except LookupError:
        text = raw[: limit * 4].decode("utf-8", "replace")
    return clean(text, limit)


def parse(uid: int, headers: bytes, body: bytes, *, body_chars: int) -> Message:
    """The message from its fetched header fields and body start (`body_chars` characters of
    text are kept)."""
    source = headers[:MAX_HEADER_BYTES].rstrip(b"\r\n") + b"\r\n\r\n" + body
    try:
        msg = email.message_from_bytes(source, policy=email.policy.default)
    except Exception:
        return Message(uid, "", "", "", "", None, "")
    address = parseaddr(_header(msg, "From"))[1].lower()
    raw_list_id = _header(msg, "List-Id")
    found = _LIST_ID.search(raw_list_id)
    return Message(
        uid=uid,
        sender=clean(address, 320),
        subject=_header(msg, "Subject"),
        list_id=(found.group(1) if found else raw_list_id[:255]).lower(),
        message_id=_header(msg, "Message-ID").strip("<> "),
        day=_day(_header(msg, "Date")),
        text=_body_text(msg, body_chars),
    )
