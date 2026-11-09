# SPDX-License-Identifier: Apache-2.0
"""Parsing what a fetch returns: hostile headers and bodies never raise."""

import base64
from datetime import date

from horae_imap import message


def parse(headers: str, body: str = "", *, chars: int = 200) -> message.Message:
    return message.parse(7, headers.encode(), body.encode(), body_chars=chars)


def test_the_headers_and_the_text() -> None:
    found = parse(
        "From: Ada <Ada@School.Example.org>\r\nSubject: Re: forms\r\n"
        "Date: Mon, 02 Nov 2026 10:00:00 +0100\r\nMessage-ID: <abc@example.org>\r\n"
        "List-Id: Class 3 <class3.school.example.org>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n",
        "Please reply by 15/11/2026.\r\n",
    )
    assert found.uid == 7
    assert found.sender == "ada@school.example.org"  # the address, never the display name
    assert found.subject == "Re: forms"
    assert found.list_id == "class3.school.example.org"
    assert found.message_id == "abc@example.org"
    assert found.day == date(2026, 11, 2)
    assert found.text == "Please reply by 15/11/2026."


def test_encoded_words_and_charsets() -> None:
    found = parse(
        "From: a@example.org\r\nSubject: =?utf-8?B?"
        + base64.b64encode("Réunion".encode()).decode()
        + "?=\r\nContent-Type: text/plain; charset=iso-8859-1\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n",
        "Caf=E9\r\n",
    )
    assert found.subject == "Réunion"
    assert found.text == "Café"


def test_multipart_takes_the_plain_part_and_a_cut_one_is_fine() -> None:
    headers = (
        "From: a@example.org\r\nSubject: s\r\nContent-Type: multipart/alternative; boundary=XX\r\n"
    )
    body = (
        "--XX\r\nContent-Type: text/plain\r\n\r\nplain words\r\n"
        "--XX\r\nContent-Type: text/html\r\n\r\n<b>html words</b>\r\n--XX--\r\n"
    )
    assert parse(headers, body).text == "plain words"
    cut = parse(headers, body[:60]).text  # the fetch stopped inside the first part
    assert cut.startswith("plain")


def test_html_only_gives_no_text() -> None:
    found = parse("From: a@example.org\r\nContent-Type: text/html\r\n", "<p>hello</p>")
    assert found.text == ""


def test_hostile_values_are_cleaned_and_capped() -> None:
    found = parse(
        "From: a@example.org\r\nSubject: bad\x00\x1b[31m ‮evil​ \U0001f4c5 2020-01-01\r\n"
        "Date: not a date\r\nMessage-ID: <" + "x" * 5000 + ">\r\n",
        "line\x00 one\r\n\r\n\r\ntwo\ttab " + "y" * 1000,
        chars=50,
    )
    assert "\x00" not in found.subject
    assert "\x1b" not in found.subject
    assert "‮" not in found.subject
    assert "​" not in found.subject
    assert found.day is None
    assert len(found.message_id) <= message.MAX_FIELD
    assert len(found.text) <= 50
    assert "\n" not in found.text


def test_garbage_never_raises() -> None:
    assert parse("").subject == ""
    assert parse("\x00\x01\x02 not headers at all").sender == ""
    found = message.parse(1, b"\xff\xfe\xfd: \xff", b"\xff" * 10, body_chars=20)
    assert found.uid == 1
    found = parse("From: a@example.org\r\nContent-Type: text/plain; charset=nope-9\r\n", "abc")
    assert found.text == "abc"


def test_list_id_without_brackets() -> None:
    assert parse("List-Id: News.Example.org\r\n").list_id == "news.example.org"
    assert parse("From: nobody\r\n").list_id == ""
    assert parse("Date: Mon, 99 Foo 2026 99:99:99 +0000\r\n").day is None
