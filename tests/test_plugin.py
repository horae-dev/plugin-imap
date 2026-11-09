# SPDX-License-Identifier: Apache-2.0
"""The source against a fake session: since-state, baselines, caps, rules, tasks, links,
host and path approval, secrets and hostile mail. The real-server tests are in
`test_greenmail.py`."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from horae_sdk.tasks import is_task_link

from conftest import ENV, PASSWORD, FakeFolder, Harness, load_state, raw

Make = Callable[[Mapping[str, FakeFolder]], Harness]

SCHOOL = {
    "name": "school",
    "kind": "request",
    "from": ["*@school.example.org"],
    "task_prefix": "Reply: ",
}
NEWS = {"name": "news", "kind": "date", "list_id": ["news.example.org"]}


def mail(n: int, **kwargs: Any) -> tuple[int, tuple[bytes, bytes] | None]:
    return n, raw(subject=kwargs.pop("subject", f"Form {n}"), **kwargs)


def inbox(count: int = 5, validity: int = 1000) -> dict[str, FakeFolder]:
    return {"INBOX": FakeFolder(validity, dict(mail(n) for n in range(1, count + 1)))}


def errors(h: Harness, **params: Any) -> str:
    result = h.run(**params)
    assert result.error is not None
    return result.error


def test_first_run_baselines_without_a_flood(harness: Make) -> None:
    h = harness(inbox())
    result = h.run(rules=[SCHOOL])
    assert result.error is None
    assert result.data["items"] == []
    assert h.session.fetched == []  # not a single message was read
    folder = result.data["folders"][0]
    assert (folder["baselined"], folder["uidvalidity_changed"], folder["last_uid"]) == (
        True,
        False,
        5,
    )
    assert load_state(h.state)["accounts"]["mail"]["INBOX"] == {"uidvalidity": 1000, "last_uid": 5}
    assert h.session.logged_out


def test_baseline_messages_handles_only_the_newest(harness: Make) -> None:
    h = harness(inbox(10))
    result = h.run(rules=[SCHOOL], baseline_messages=3)
    assert [i["uid"] for i in result.data["items"]] == [8, 9, 10]
    assert h.session.fetched == [8, 9, 10]
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 10


def test_a_run_fetches_only_new_mail(harness: Make) -> None:
    h = harness(inbox(3))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].messages.update([mail(4), mail(5)])
    h.session.log.clear()
    result = h.run(rules=[SCHOOL])
    assert [i["uid"] for i in result.data["items"]] == [4, 5]
    assert h.session.fetched == [4, 5]
    folder = result.data["folders"][0]
    assert (folder["baselined"], folder["fetched"], folder["more"], folder["last_uid"]) == (
        False,
        2,
        False,
        5,
    )
    h.session.log.clear()
    again = h.run(rules=[SCHOOL])
    assert again.data["items"] == []
    assert h.session.fetched == []


def test_gaps_in_uids_are_fine(harness: Make) -> None:
    h = harness({"INBOX": FakeFolder(1000, dict([mail(2), mail(9)]), uidnext=40)})
    h.run(rules=[SCHOOL])
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 39
    h.session.folders["INBOX"].messages[40] = raw(subject="later")
    h.session.folders["INBOX"].uidnext = 41
    assert [i["uid"] for i in h.run(rules=[SCHOOL]).data["items"]] == [40]


def test_a_changed_uidvalidity_rebaselines_safely(harness: Make) -> None:
    h = harness(inbox(4))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"] = FakeFolder(2000, dict(mail(n) for n in range(1, 9)))
    h.session.log.clear()
    result = h.run(rules=[SCHOOL], baseline_messages=2)
    folder = result.data["folders"][0]
    assert folder["uidvalidity_changed"] is True
    assert folder["baselined"] is True
    # the old last_uid (4) would have meant "fetch 5 to 8": never, UIDs mean something else now
    assert h.session.fetched == [7, 8]
    assert load_state(h.state)["accounts"]["mail"]["INBOX"] == {"uidvalidity": 2000, "last_uid": 8}
    # ... and the next run is an ordinary one
    h.session.folders["INBOX"].messages[9] = raw(subject="new")
    h.session.log.clear()
    assert [i["uid"] for i in h.run(rules=[SCHOOL]).data["items"]] == [9]


def test_uids_going_backwards_rebaseline(harness: Make) -> None:
    h = harness(inbox(6))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"] = FakeFolder(1000, dict(mail(n) for n in range(1, 3)))
    result = h.run(rules=[SCHOOL])
    assert result.data["folders"][0]["baselined"] is True
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 2


def test_a_rebaseline_is_capped_by_the_run_cap(harness: Make) -> None:
    h = harness(inbox(30))
    result = h.run(rules=[SCHOOL], baseline_messages=20, max_messages=20)
    assert len(result.data["items"]) == 20
    assert len(h.session.fetched) == 20
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 30


def test_the_per_run_cap_resumes_where_it_stopped(harness: Make) -> None:
    h = harness(inbox(2))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].messages.update(mail(n) for n in range(3, 10))
    first = h.run(rules=[SCHOOL], max_messages=3)
    assert [i["uid"] for i in first.data["items"]] == [3, 4, 5]
    assert first.data["truncated"] is True
    second = h.run(rules=[SCHOOL], max_messages=3)
    assert [i["uid"] for i in second.data["items"]] == [6, 7, 8]
    third = h.run(rules=[SCHOOL], max_messages=3)
    assert [i["uid"] for i in third.data["items"]] == [9]
    assert third.data["truncated"] is False


def test_the_cap_is_shared_by_the_folders(harness: Make) -> None:
    folders = {
        "INBOX": FakeFolder(1, dict(mail(n) for n in range(1, 4))),
        "Lists": FakeFolder(2, dict(mail(n) for n in range(1, 4))),
    }
    h = harness(folders)
    h.run(rules=[SCHOOL], folders=["INBOX", "Lists"])
    for folder in folders.values():
        folder.messages.update(mail(n) for n in range(4, 8))
    result = h.run(rules=[SCHOOL], folders=["INBOX", "Lists"], max_messages=5)
    assert [(i["folder"], i["uid"]) for i in result.data["items"]] == [
        ("INBOX", 4),
        ("INBOX", 5),
        ("INBOX", 6),
        ("INBOX", 7),
        ("Lists", 4),
    ]
    assert result.data["truncated"] is True
    assert load_state(h.state)["accounts"]["mail"]["Lists"]["last_uid"] == 4


def test_the_time_budget_stops_cleanly_and_resumes(harness: Make) -> None:
    h = harness(inbox(2))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].messages.update(mail(n) for n in range(3, 8))
    h.step = 1.0
    result = h.run(rules=[SCHOOL], budget_s=3)
    done = [i["uid"] for i in result.data["items"]]
    assert 0 < len(done) < 5
    assert result.data["truncated"] is True
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == done[-1]
    h.step = 0.0
    rest = [i["uid"] for i in h.run(rules=[SCHOOL]).data["items"]]
    assert done + rest == [3, 4, 5, 6, 7]


def test_a_lying_uidnext_cannot_make_a_run_scan_forever(harness: Make) -> None:
    h = harness({"INBOX": FakeFolder(1, dict(mail(n) for n in range(1, 4)))})
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].uidnext = 10**9
    h.session.log.clear()
    result = h.run(rules=[SCHOOL])
    searches = [v for name, v in h.session.log if name == "uids"]
    assert 0 < len(searches) <= 1001
    assert all(high - low < 2000 for low, high in searches)
    assert result.data["truncated"] is True  # not "done": the state did not jump ahead
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 3
    # a baseline against the same lie is cut too, and never walks the whole range
    fresh_run = harness({"INBOX": FakeFolder(1, {5: raw()}, uidnext=10**9)})
    cut = fresh_run.run(rules=[SCHOOL], baseline_messages=5)
    assert cut.error is None
    assert len([v for name, v in fresh_run.session.log if name == "uids"]) <= 1001


def test_the_time_budget_also_bounds_the_search(harness: Make) -> None:
    h = harness(inbox(2))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].messages.update(mail(n) for n in range(3, 6))
    h.step = 10.0  # every clock reading is 10 s later: out of time before the first search
    result = h.run(rules=[SCHOOL], budget_s=5)
    assert result.data["truncated"] is True
    assert result.data["items"] == []
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 2
    h.step = 0.0
    assert [i["uid"] for i in h.run(rules=[SCHOOL]).data["items"]] == [3, 4, 5]


def test_a_message_expunged_meanwhile_is_skipped(harness: Make) -> None:
    h = harness(
        {"INBOX": FakeFolder(1, {1: raw(), 2: None, 3: raw(sender="a@school.example.org")})}
    )
    h.run(rules=[SCHOOL], baseline_messages=3)
    h.session.folders["INBOX"].messages[2] = None
    # (the fake lists uid 2 but cannot fetch it)
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 3


def test_servers_that_give_no_uidnext(harness: Make) -> None:
    h = harness({"INBOX": FakeFolder(1, dict(mail(n) for n in range(1, 4)), uidnext=-1)})
    h.run(rules=[SCHOOL])
    assert load_state(h.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 3
    empty = harness({"INBOX": FakeFolder(1, {}, uidnext=-1)})
    empty.run(rules=[SCHOOL])
    assert load_state(empty.state)["accounts"]["mail"]["INBOX"]["last_uid"] == 0


def test_update_state_false_leaves_no_file(harness: Make) -> None:
    h = harness(inbox())
    result = h.run(rules=[SCHOOL], update_state=False)
    assert result.data["state_updated"] is False
    assert not h.state.exists()


def test_a_failing_folder_writes_nothing(harness: Make) -> None:
    h = harness(inbox())
    message = errors(h, rules=[SCHOOL], folders=["INBOX", "Missing"])
    assert "folder_unavailable" in message
    assert not h.state.exists()
    assert h.session.logged_out


def test_accounts_keep_separate_state(harness: Make) -> None:
    h = harness(inbox(3))
    h.run(rules=[SCHOOL], account="home")
    h.run(rules=[SCHOOL], account="work")
    assert set(load_state(h.state)["accounts"]) == {"home", "work"}


# -- rules, items and tasks ------------------------------------------------------------------


def test_items_tasks_and_fingerprints(harness: Make) -> None:
    h = harness(inbox(1))
    h.run(rules=[SCHOOL, NEWS])
    h.session.folders["INBOX"].messages.update(
        {
            2: raw(
                "teacher@school.example.org",
                "Autorisation",
                "Merci de répondre avant le 15/11/2026.",
            ),
            3: raw("bob@example.org", "Lunch", "no rule matches"),
            4: raw(
                "x@example.org", "Newsletter", "Fête le 12 décembre", list_id="<news.example.org>"
            ),
            5: raw("x@example.org", "Newsletter bis", "no date", list_id="<news.example.org>"),
        }
    )
    result = h.run(rules=[SCHOOL, NEWS], emit_tasks=True, snippet_chars=20)
    first, second = result.data["items"]
    assert first["fingerprint"] == "imap:request:mail:INBOX:1000:2"
    assert (first["kind"], first["rule"], first["due"], first["dates"]) == (
        "request",
        "school",
        "2026-11-15",
        ["2026-11-15"],
    )
    assert first["from"] == "teacher@school.example.org"
    assert first["received"] == "2026-11-02"
    assert len(first["snippet"]) <= 28
    assert "link" not in first
    assert (second["kind"], second["due"]) == ("date", "2026-12-12")
    assert result.intents == [
        {
            "type": "task.create",
            "title": "Reply: Autorisation",
            "fingerprint": "imap:request:mail:INBOX:1000:2",
            "due": "2026-11-15",
        },
        {
            "type": "task.create",
            "title": "Newsletter",
            "fingerprint": "imap:date:mail:INBOX:1000:4",
            "due": "2026-12-12",
        },
    ]


def test_tasks_only_with_emit_tasks_and_items_never_need_them(harness: Make) -> None:
    h = harness(inbox(1))
    h.run(rules=[SCHOOL])
    h.session.folders["INBOX"].messages[2] = raw("a@school.example.org", "s")
    result = h.run(rules=[SCHOOL])
    assert len(result.data["items"]) == 1
    assert result.intents == []


def test_the_message_id_fingerprint_survives_a_uidvalidity_change(harness: Make) -> None:
    def keys(validity: int, uid: int) -> str:
        h = harness(
            {
                "INBOX": FakeFolder(
                    validity, {uid: raw("a@school.example.org", "s", message_id="m1@example.org")}
                )
            }
        )
        result = h.run(rules=[SCHOOL], baseline_messages=1, fingerprint_by="message_id")
        return str(result.data["items"][0]["fingerprint"])

    assert keys(1, 3) == keys(2, 9)
    assert keys(1, 3).startswith("imap:request:mail:mid:")
    by_uid = harness({"INBOX": FakeFolder(5, {4: raw("a@school.example.org", "s")})})
    only = by_uid.run(rules=[SCHOOL], baseline_messages=1, fingerprint_by="message_id")
    assert only.data["items"][0]["fingerprint"] == "imap:request:mail:INBOX:5:4"  # no Message-ID


def test_a_link_only_when_configured_and_only_from_the_template(harness: Make) -> None:
    folders = {
        "Lists/School": FakeFolder(
            3, {1: raw("a@school.example.org", "s", "see https://evil.example.net/x")}
        )
    }
    plain = harness(folders).run(
        rules=[SCHOOL], folders=["Lists/School"], baseline_messages=1, emit_tasks=True
    )
    assert "link" not in plain.data["items"][0]
    assert "link" not in plain.intents[0]
    result = harness(folders).run(
        rules=[SCHOOL],
        folders=["Lists/School"],
        baseline_messages=1,
        emit_tasks=True,
        link_template="https://mail.example.org/#/{folder}/{uid}?v={uidvalidity}",
    )
    link = "https://mail.example.org/#/Lists%2FSchool/1?v=3"
    assert result.data["items"][0]["link"] == link
    assert result.intents[0]["link"] == link
    assert is_task_link(link)
    assert "evil" not in str(result.intents)


@pytest.mark.parametrize(
    "template",
    [
        "http://mail.example.org/{uid}",
        "https://mail.example.org/",
        "https://127.0.0.1/{uid}",
        "https://user@mail.example.org/{uid}",
        "mail/{uid}",
        5,
        "https://mail.example.org/{uid} x",
    ],
)
def test_bad_link_templates_are_refused(harness: Make, template: Any) -> None:
    assert "link_template" in errors(harness(inbox()), rules=[SCHOOL], link_template=template)


def test_hostile_mail_is_neutralised_in_data_and_tasks(harness: Make) -> None:
    hostile = raw(
        "attacker@school.example.org",
        "Pay now \U0001f4c5 2020-01-01 \U0001f501 every day [[wiki]] **bold** https://evil.example.net",
        "<script>x</script> \U0001f4c5 2020-02-02 [[Secret]] \x00\x1b[2J http://evil.example.net/p "
        + "A" * 5000,
    )
    h = harness({"INBOX": FakeFolder(1, {1: hostile})})
    result = h.run(rules=[SCHOOL], baseline_messages=1, emit_tasks=True, snippet_chars=80)
    item = result.data["items"][0]
    for text in (item["subject"], item["snippet"], result.intents[0]["title"]):
        assert "\U0001f4c5" not in text
        assert "\U0001f501" not in text
        assert "[[" not in text
        assert "**" not in text
        assert "\x00" not in text
        assert "\x1b" not in text
        assert "https://" not in text.replace("https\\://", "")
    assert len(item["snippet"]) <= 88
    assert "A" * 100 not in str(result.data)  # a raw body never reaches the output
    assert len(str(result.data)) < 5000


def test_snippets_can_be_turned_off(harness: Make) -> None:
    h = harness({"INBOX": FakeFolder(1, {1: raw("a@school.example.org", "s", "body words")})})
    item = h.run(rules=[SCHOOL], baseline_messages=1, snippet_chars=0).data["items"][0]
    assert "snippet" not in item
    assert "body words" not in str(item)


def test_the_body_cap_reaches_the_server(harness: Make) -> None:
    h = harness(
        {
            "INBOX": FakeFolder(
                1, {1: raw("a@school.example.org", "s", "x" * 5000 + " le 1 mars 2027")}
            )
        }
    )
    item = h.run(rules=[SCHOOL], baseline_messages=1, max_body_kib=1).data["items"][0]
    assert item["dates"] == []  # past the first KiB: never read


def test_a_run_is_deterministic(harness: Make) -> None:
    def once() -> Any:
        h = harness(
            {
                "INBOX": FakeFolder(
                    1, dict(mail(n, sender="a@school.example.org") for n in range(1, 6))
                )
            }
        )
        return h.run(rules=[SCHOOL], baseline_messages=5, emit_tasks=True)

    first, second = once(), once()
    assert first.data == second.data
    assert first.intents == second.intents


# -- connection and approval -----------------------------------------------------------------


def test_the_endpoint_defaults_and_secrets(harness: Make) -> None:
    h = harness(inbox())
    h.run(rules=[SCHOOL])
    endpoint, user, password = h.endpoints[0]
    assert (endpoint.host, endpoint.port, endpoint.security) == (HOST_NAME, 993, "ssl")
    assert 0 < endpoint.timeout <= 60
    assert (user, password) == (ENV["IMAP_USERNAME"], ENV["IMAP_PASSWORD"])
    h.run(rules=[SCHOOL], security="starttls", port=1143)
    assert (h.endpoints[1][0].port, h.endpoints[1][0].security) == (1143, "starttls")


HOST_NAME = "imap.fastmail.com"


def test_plain_text_needs_the_explicit_flag(harness: Make) -> None:
    h = harness(inbox())
    assert "allow_plaintext" in errors(
        h, security="plain", host="localhost", allow_loopback=True, port=1143, network=None
    )
    assert h.endpoints == []
    ok = h.run(
        security="plain",
        host="localhost",
        allow_plaintext=True,
        allow_loopback=True,
        network=None,
        rules=[SCHOOL],
        port=1143,
    )
    assert ok.error is None
    assert h.endpoints[0][0].security == "plain"
    assert h.endpoints[0][0].port == 1143


def test_hosts_must_be_declared_or_listed_and_approved(harness: Make) -> None:
    h = harness(inbox())
    assert "host_not_approved: mail.example.org" in errors(h, host="mail.example.org")
    assert h.endpoints == []
    listed = h.run(
        host="mail.example.org", allowed_hosts=["mail.example.org"], network=["mail.example.org"]
    )
    assert listed.error is None
    # listed by the pipeline but not approved in the lock: refused
    assert "host_not_approved" in errors(
        h, host="mail.example.org", allowed_hosts=["mail.example.org"], network=[]
    )
    assert "host_not_approved" in errors(h, network=[])  # a declared host, not in the lock
    # an older core sends no list: the declared and listed hosts decide
    assert h.run(network=None, host="imap.gmail.com", rules=[SCHOOL]).error is None


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "192.168.1.5",
        "mail.example.org:993",
        "Mail.Example.org",
        "a b",
        "",
        None,
        "::2",
        "[::1]",
    ],
)
def test_ip_literals_ports_and_odd_names_are_refused(harness: Make, host: Any) -> None:
    h = harness(inbox())
    assert "config_invalid" in errors(h, host=host, allowed_hosts=["mail.example.org"])
    assert h.endpoints == []


def test_bad_allowed_hosts_are_refused(harness: Make) -> None:
    h = harness(inbox())
    for value in ("x", ["127.0.0.1"], ["localhost"], ["A.example.org"], ["a"] * 17, [5]):
        assert "allowed_hosts" in errors(h, allowed_hosts=value)


def test_loopback_is_for_a_local_bridge(harness: Make) -> None:
    h = harness(inbox())
    result = h.run(
        host="127.0.0.1",
        security="starttls",
        port=1143,
        allow_loopback=True,
        network=None,
        rules=[SCHOOL],
    )
    assert result.error is None
    assert h.endpoints[0][0].host == "127.0.0.1"


def test_the_state_path_must_be_approved(harness: Make, tmp_path: Path) -> None:
    h = harness(inbox())
    assert "path_not_approved" in errors(h, paths=[])
    assert "path_not_approved" in errors(h, paths=[str(tmp_path / "elsewhere")])
    assert "path_not_approved" in errors(h, state_path=str(tmp_path))  # a directory
    assert "path_not_approved" in errors(h, state_path="relative/state.json")
    assert "state_path" in errors(h, state_path=None)
    assert h.endpoints == []


def test_a_missing_secret_names_it_and_connects_to_nothing(tmp_path: Path) -> None:
    from conftest import FakeSession
    from horae_imap import build

    session = FakeSession({})
    for missing in ("IMAP_USERNAME", "IMAP_PASSWORD"):
        environ = {k: v for k, v in ENV.items() if k != missing}
        calls: list[str] = []

        def connector(*args: Any, calls: list[str] = calls) -> FakeSession:
            calls.append("connect")
            return session

        h = Harness(tmp_path, {})
        h.plugin = build(connector, environ=environ)
        message = errors(h)
        assert f"secret_missing: {missing}" in message
        assert calls == []
        assert PASSWORD not in message


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"security": "tls"}, "security"),
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"timeout_s": 0}, "timeout_s"),
        ({"timeout_s": 61}, "timeout_s"),
        ({"budget_s": 0}, "budget_s"),
        ({"max_messages": 0}, "max_messages"),
        ({"max_messages": 201}, "max_messages"),
        ({"max_body_kib": 0}, "max_body_kib"),
        ({"max_body_kib": 65}, "max_body_kib"),
        ({"baseline_messages": -1}, "baseline_messages"),
        ({"baseline_messages": 51}, "baseline_messages"),
        ({"snippet_chars": 501}, "snippet_chars"),
        ({"folders": []}, "folders"),
        ({"folders": ["INBOX", "INBOX"]}, "folders"),
        ({"folders": [1]}, "folders"),
        ({"folders": [f"f{n}" for n in range(9)]}, "folders"),
        ({"account": "bad name"}, "account"),
        ({"fingerprint_by": "subject"}, "fingerprint_by"),
        ({"date_order": "ymd"}, "date_order"),
        ({"rules": "all"}, "rules"),
    ],
)
def test_bad_parameters_are_refused_by_name(
    harness: Make, params: dict[str, Any], needle: str
) -> None:
    h = harness(inbox())
    assert needle in errors(h, **params)
    assert h.endpoints == []
    assert not h.state.exists()


LOOPBACK_ARGS: dict[str, Any] = {"security": "starttls", "port": 1143, "rules": [SCHOOL]}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_needs_the_explicit_flag_with_an_older_core(harness: Make, host: str) -> None:
    h = harness(inbox())
    assert "allow_loopback" in errors(h, host=host, network=None, **LOOPBACK_ARGS)
    assert "allow_loopback" in errors(
        h, host=host, allow_loopback=False, network=None, **LOOPBACK_ARGS
    )
    assert h.endpoints == []
    assert h.run(host=host, allow_loopback=True, network=None, **LOOPBACK_ARGS).error is None


@pytest.mark.parametrize(
    ("host", "grant"), [("127.0.0.1", "127.0.0.1:1143"), ("::1", "[::1]:1143")]
)
def test_a_network_local_grant_approves_a_bridge(harness: Make, host: str, grant: str) -> None:
    h = harness(inbox())
    result = h.run(host=host, network=[], network_local=[grant], **LOOPBACK_ARGS)
    assert result.error is None
    assert h.endpoints[0][0].host == host
    assert h.endpoints[0][0].port == 1143


def test_a_grant_makes_allow_loopback_unnecessary_but_harmless(harness: Make) -> None:
    h = harness(inbox())
    for flag in (False, True):
        result = h.run(
            host="127.0.0.1",
            allow_loopback=flag,
            network_local=["127.0.0.1:1143"],
            **LOOPBACK_ARGS,
        )
        assert result.error is None


def test_loopback_is_refused_when_the_lock_says_no(harness: Make) -> None:
    h = harness(inbox())
    # a core that sends lists but no matching grant: the opt-in does not override it
    for network, local in (([], []), (["imap.fastmail.com"], []), ([], ["127.0.0.1:2143"])):
        message = errors(
            h,
            host="127.0.0.1",
            allow_loopback=True,
            network=network,
            network_local=local,
            **LOOPBACK_ARGS,
        )
        assert "host_not_approved" in message
        assert "network_local" in message
    assert h.endpoints == []


def test_a_grant_for_another_host_or_port_is_refused(harness: Make) -> None:
    h = harness(inbox())
    for grant in ("127.0.0.1:1144", "127.0.0.2:1143", "[::1]:1143", "imap.lan:1143"):
        message = errors(h, host="127.0.0.1", network=[], network_local=[grant], **LOOPBACK_ARGS)
        assert "host_not_approved" in message
    assert "host_not_approved" in errors(
        h, host="localhost", network=[], network_local=["127.0.0.1:1143"], **LOOPBACK_ARGS
    )
    assert h.endpoints == []


def test_a_local_grant_never_approves_a_public_name(harness: Make) -> None:
    h = harness(inbox())
    message = errors(
        h,
        host="mail.example.org",
        allowed_hosts=["mail.example.org"],
        network=[],
        network_local=["127.0.0.1:1143"],
    )
    assert "host_not_approved" in message
    assert h.endpoints == []


def test_loopback_needs_an_explicit_port(harness: Make) -> None:
    h = harness(inbox())
    args = {k: v for k, v in LOOPBACK_ARGS.items() if k != "port"}
    message = errors(h, host="127.0.0.1", allow_loopback=True, network=None, **args)
    assert "explicit port" in message
    assert h.endpoints == []


@pytest.mark.parametrize(("answer", "loopback"), [(True, False), (True, True), (None, True)])
def test_loopback_accepted_when_the_lock_agrees_or_is_silent(
    answer: bool | None, loopback: bool
) -> None:
    from horae_sdk import Call

    from horae_imap.plugin import _check_host

    class Asked(Call):
        asked: list[tuple[str, int]]

        def address_allowed(self, host: str, port: int) -> bool | None:
            self.asked = [*getattr(self, "asked", []), (host, port)]
            return answer

    call = Asked(tool="imap.messages")
    assert _check_host(call, "127.0.0.1", frozenset(), loopback=loopback, port=1143) == "127.0.0.1"
    assert _check_host(call, "::1", frozenset(), loopback=loopback, port=1143) == "::1"
    assert call.asked == [("127.0.0.1", 1143), ("::1", 1143)]


def test_the_lock_is_asked_about_the_configured_port_only(harness: Make) -> None:
    h = harness(inbox())
    assert h.run(host="127.0.0.1", allow_loopback=True, network=None, **LOOPBACK_ARGS).error is None
    assert h.endpoints[0][0].port == 1143
    assert "config_invalid" in errors(
        h, host="127.0.0.1", allow_loopback=True, network=None, **{**LOOPBACK_ARGS, "port": 70000}
    )
