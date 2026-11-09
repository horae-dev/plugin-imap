# SPDX-License-Identifier: Apache-2.0
"""The plugin: one source, `imap.messages`, the new mail of some IMAP folders, read-only.

For each folder it reads the messages that arrived since the last run (per folder: UIDVALIDITY
and the last UID handled, kept in a state file the core approved for writing), applies the
configured rules (`horae_imap.rules`: sender, subject and list-id globs, deterministic, no LLM)
and returns the matching messages as `request` and `date` items. With `emit_tasks` it also
proposes a `task.create` for each, with a stable fingerprint and, only when `link_template`
is configured, a link built from that template.

**Never alters the mailbox** (`horae_imap.session`): `EXAMINE`, not `SELECT`; `BODY.PEEK[...]`
only, so `\\Seen` is never set; no `STORE`, `EXPUNGE`, `COPY` or `MOVE` exists in the package.

**Trusted and untrusted.** The tool's parameters come from the pipeline (trusted): the server,
the folders, the rules, the caps. The credentials are two declared secrets, read at call time and
never logged, returned or put in an error. Everything in a message is untrusted: a subject or a
snippet reaches the output only escaped and capped, a raw body never does, and nothing in a
message is ever fetched or followed (a link in a mail is never opened and never becomes a task
link).

**Failures.** Every expected failure raises `PluginError` with a fixed code
(`config_invalid`, `secret_missing`, `host_not_approved`, `connect_failed`, `tls_failed`,
`login_failed`, `folder_unavailable`, `protocol_error`, `timeout`, `state_*`) and nothing is
written: the state file is replaced only after every folder was read. Reaching the time budget
or a cap is not a failure: the result says `truncated` and the next run continues.
"""

import hashlib
import ipaddress
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from horae_sdk import Call, Output, Permissions, Plugin, PluginError, intents, tasks
from horae_sdk.tasks import is_task_link

from horae_imap import message as messages
from horae_imap import rules as rule_engine
from horae_imap import state as state_file
from horae_imap.session import Connector, Endpoint, Selected, Session, connect

NAME = "imap"
"""The plugin's name, also the namespace of its refs (`imap.messages`) and fingerprints."""
VERSION = "0.1.0"
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_imap"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
HOSTS = ("imap.fastmail.com", "imap.gmail.com")
"""The hosts declared in `plugin.yaml`. Any other server is approved per instance: declare it
in the lock and list it in the call's `allowed_hosts` (README, "Other hosts")."""
USERNAME_SECRET = "IMAP_USERNAME"  # noqa: S105 (a secret's name, not its value)
PASSWORD_SECRET = "IMAP_PASSWORD"  # noqa: S105 (a secret's name, not its value)
PERMISSIONS = Permissions(
    intents=["task.create"],
    network=list(HOSTS),
    secrets=[USERNAME_SECRET, PASSWORD_SECRET],
)
"""Network: the providers above. Secrets: the login. Paths: none declared (the state file's
directory is granted per instance in `horae.lock`). Intents: `task.create`, only with
`emit_tasks`."""

MAX_FOLDERS = 8
MAX_MESSAGES = 200
"""Per call, all folders together (the SDK refuses more than 256 intents)."""
MAX_BODY_KIB = 64
MAX_ALLOWED_HOSTS = 16
WINDOW = 2000
"""UIDs asked in one `UID SEARCH`: the answer stays small whatever the mailbox holds."""
MAX_WINDOWS = 1000
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
PLACEHOLDERS = ("{folder}", "{uid}", "{uidvalidity}", "{message_id_hash}")
FINGERPRINTS = ("uid", "message_id")

Clock = Callable[[], float]


@dataclass(frozen=True)
class Options:
    """The validated parameters of one call."""

    endpoint: Endpoint
    label: str
    folders: tuple[str, ...]
    state_path: str
    update_state: bool
    max_messages: int
    body_kib: int
    baseline_messages: int
    rules: tuple[rule_engine.Rule, ...]
    order: str
    snippet_chars: int
    emit_tasks: bool
    link_template: str | None
    fingerprint_by: str
    budget_s: float


def _bounded(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise PluginError(f"config_invalid: {name} is a whole number from {low} to {high}")
    return value


def _allowed_hosts(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > MAX_ALLOWED_HOSTS:
        raise PluginError(f"config_invalid: allowed_hosts lists at most {MAX_ALLOWED_HOSTS} hosts")
    for host in value:
        if not isinstance(host, str) or len(host) > 253 or not _is_dns_name(host):
            raise PluginError(
                "config_invalid: allowed_hosts: each host is a lowercase DNS name (no IP, no port)"
            )
    return frozenset(value)


def _is_dns_name(host: str) -> bool:
    if not HOST.fullmatch(host) or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _check_host(
    call: Call, host: object, allowed: frozenset[str], *, loopback: bool, port: int | None
) -> str:
    """The server's host, once it is allowed to be contacted.

    A DNS name must be declared, or listed in `allowed_hosts`, **and** not refused by the lock
    (`call.host_allowed`). A loopback name (a bridge on this machine; a manifest cannot declare
    it) needs the explicit `allow_loopback` and the configured `port`, and `call.host_allowed`
    is still asked about that host and port: `False` refuses, `None` (an older core sent no
    list) leaves the explicit opt-in as the approval."""
    if not isinstance(host, str):
        raise PluginError("config_invalid: host is required")
    if host in LOOPBACK:
        if not loopback:
            raise PluginError("host_not_approved: a loopback host needs allow_loopback: true")
        if port is None:
            raise PluginError("config_invalid: a loopback host needs an explicit port")
        name = f"[{host}]" if ":" in host else host
        if call.host_allowed(f"https://{name}:{port}/") is False:
            raise PluginError(f"host_not_approved: {host} is not approved in horae.lock")
        return host
    if not _is_dns_name(host) or len(host) > 253:
        raise PluginError("config_invalid: host is a lowercase DNS name (no IP, no port)")
    if host not in {*HOSTS, *allowed} or call.host_allowed(f"https://{host}/") is False:
        raise PluginError(f"host_not_approved: {host}")
    return host


def _link_template(value: object) -> str | None:
    if value is None:
        return None
    sample = _link(value, "sample", "1", "1", "0" * 8) if isinstance(value, str) else None
    if (
        sample is None
        or not isinstance(value, str)
        or not any(p in value for p in PLACEHOLDERS)
        or not is_task_link(sample)
    ):
        raise PluginError(
            "config_invalid: link_template must be an https URL with a host name and at least "
            f"one of {', '.join(PLACEHOLDERS)}"
        )
    return value


def _link(template: str, folder: str, uid: str, validity: str, digest: str) -> str:
    values = {
        "{folder}": quote(folder, safe=""),
        "{uid}": uid,
        "{uidvalidity}": validity,
        "{message_id_hash}": digest,
    }
    for placeholder, replacement in values.items():
        template = template.replace(placeholder, replacement)
    return template


def options(call: Call, params: Mapping[str, Any]) -> Options:
    """Check every parameter. Nothing here touches the network."""
    security = params["security"]
    if security not in ("ssl", "starttls", "plain"):
        raise PluginError("config_invalid: security is ssl, starttls or plain")
    if security == "plain" and params["allow_plaintext"] is not True:
        raise PluginError(
            "config_invalid: security plain sends the password unencrypted: "
            "set allow_plaintext: true to accept it"
        )
    port = params["port"]
    if port is not None:
        port = _bounded(port, "port", 1, 65535)
    if type(params["allow_loopback"]) is not bool:
        raise PluginError("config_invalid: allow_loopback is true or false")
    host = _check_host(
        call,
        params["host"],
        _allowed_hosts(params["allowed_hosts"]),
        loopback=params["allow_loopback"],
        port=port,
    )
    if port is None:
        port = 993 if security == "ssl" else 143
    timeout = _bounded(params["timeout_s"], "timeout_s", 1, 60)
    budget = _bounded(params["budget_s"], "budget_s", 1, 300)
    label = params["account"]
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise PluginError("config_invalid: account is 1 to 32 of A-Z a-z 0-9 . _ -")
    folders = params["folders"]
    if folders is None:
        folders = ["INBOX"]
    if (
        not isinstance(folders, list)
        or not 1 <= len(folders) <= MAX_FOLDERS
        or not all(isinstance(f, str) for f in folders)
        or len(set(folders)) != len(folders)
    ):
        raise PluginError(f"config_invalid: folders lists 1 to {MAX_FOLDERS} distinct names")
    state_path = params["state_path"]
    if not isinstance(state_path, str):
        raise PluginError("config_invalid: state_path is required (an absolute path)")
    real = call.paths.allowed(state_path, write=True)
    if real is None or os.path.isdir(real):
        raise PluginError("path_not_approved: state_path is not under a path approved for writing")
    if params["fingerprint_by"] not in FINGERPRINTS:
        raise PluginError("config_invalid: fingerprint_by is uid or message_id")
    if params["date_order"] not in ("dmy", "mdy"):
        raise PluginError("config_invalid: date_order is dmy or mdy")
    for flag in ("update_state", "emit_tasks"):
        if type(params[flag]) is not bool:
            raise PluginError(f"config_invalid: {flag} is true or false")
    max_messages = _bounded(params["max_messages"], "max_messages", 1, MAX_MESSAGES)
    return Options(
        endpoint=Endpoint(host, port, security, float(timeout)),
        label=label,
        folders=tuple(folders),
        state_path=real,
        update_state=params["update_state"],
        max_messages=max_messages,
        body_kib=_bounded(params["max_body_kib"], "max_body_kib", 1, MAX_BODY_KIB),
        baseline_messages=_bounded(
            params["baseline_messages"], "baseline_messages", 0, max_messages
        ),
        rules=rule_engine.parse_rules(params["rules"]),
        order=params["date_order"],
        snippet_chars=_bounded(params["snippet_chars"], "snippet_chars", 0, 500),
        emit_tasks=params["emit_tasks"],
        link_template=_link_template(params["link_template"]),
        fingerprint_by=params["fingerprint_by"],
        budget_s=float(budget),
    )


@dataclass
class FolderResult:
    folder: str
    uidvalidity: int
    last_uid: int
    fetched: int = 0
    matched: int = 0
    baselined: bool = False
    uidvalidity_changed: bool = False
    more: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "uidvalidity": self.uidvalidity,
            "last_uid": self.last_uid,
            "fetched": self.fetched,
            "matched": self.matched,
            "baselined": self.baselined,
            "uidvalidity_changed": self.uidvalidity_changed,
            "more": self.more,
        }


@dataclass
class Collected:
    items: list[dict[str, Any]]
    intents: list[dict[str, Any]]

    def __init__(self) -> None:
        self.items, self.intents = [], []


def _scan(
    session: Session,
    low: int,
    high: int,
    limit: int,
    *,
    newest: bool,
    expired: Callable[[], bool],
) -> tuple[list[int], bool]:
    """Up to `limit` UIDs in `low` to `high`, the oldest first, or the newest `limit` (still
    ascending) when `newest`; a window at a time, so a huge mailbox is never listed whole.

    The second value is true when the scan was cut short (out of time, or `MAX_WINDOWS`
    windows) before it had `limit` UIDs or reached the end of the range."""
    found: list[int] = []
    cut = False
    for _ in range(MAX_WINDOWS + 1):
        if low > high or len(found) >= limit:
            break
        if expired() or _ == MAX_WINDOWS:
            cut = True
            break
        if newest:
            start = max(low, high - WINDOW + 1)
            found = session.uids(start, high) + found
            high = start - 1
        else:
            end = min(high, low + WINDOW - 1)
            found += session.uids(low, end)
            low = end + 1
    return (found[-limit:] if newest else found[:limit]), cut


def _read_folder(
    session: Session,
    folder: str,
    prior: state_file.FolderState | None,
    opts: Options,
    left: int,
    time_limit: tuple[float, Clock],
    out: Collected,
) -> FolderResult:
    """One folder: open it read-only, find its new UIDs, fetch and judge each message."""
    deadline, clock = time_limit

    def expired() -> bool:
        return clock() >= deadline

    selected: Selected = session.examine(folder)
    if selected.uidnext is not None:
        upper = max(selected.uidnext - 1, 0)
    else:
        upper = session.highest_uid() if selected.exists else 0
    result = FolderResult(folder, selected.uidvalidity, prior.last_uid if prior else 0)
    if prior is None or prior.uidvalidity != selected.uidvalidity or prior.last_uid > upper:
        # First run, UIDVALIDITY changed (every old UID means something else now) or UIDs went
        # backwards: start from now. The newest `baseline_messages` are still handled, never
        # the whole history.
        result.baselined = True
        result.uidvalidity_changed = prior is not None
        take = min(opts.baseline_messages, left)
        # a scan cut short only means fewer of the newest are handled: never a flood
        uids = _scan(session, 1, upper, take, newest=True, expired=expired)[0] if take > 0 else []
        result.last_uid = uids[0] - 1 if uids else upper
    else:
        uids, result.more = (
            _scan(session, prior.last_uid + 1, upper, left, newest=False, expired=expired)
            if left > 0
            else ([], False)
        )
    for uid in uids:
        if expired():
            result.more = True
            break
        result.last_uid = uid
        fetched = session.fetch(uid, body_bytes=opts.body_kib * 1024)
        if fetched is None:  # expunged since the search
            continue
        result.fetched += 1
        message = messages.parse(uid, *fetched, body_chars=opts.body_kib * 1024)
        item = rule_engine.evaluate(opts.rules, message, order=opts.order)
        if item is None:
            continue
        result.matched += 1
        _emit(out, opts, result, message, item)
    if result.more:
        return result  # out of time: the next run continues after the last UID handled
    if result.baselined:
        result.last_uid = upper
    elif left <= 0 or len(uids) >= left:
        # the per-run cap, not the end of the folder, may have stopped the scan
        more, cut = _scan(session, result.last_uid + 1, upper, 1, newest=False, expired=expired)
        result.more = bool(more) or cut
    if not result.more:
        result.last_uid = upper
    return result


def _emit(
    out: Collected,
    opts: Options,
    result: FolderResult,
    message: messages.Message,
    item: rule_engine.Item,
) -> None:
    """One item (and, with `emit_tasks`, its `task.create`). Nothing from the message but its
    escaped subject and snippet is copied."""
    digest = hashlib.sha256(message.message_id.encode("utf-8", "replace")).hexdigest()[:32]
    if opts.fingerprint_by == "message_id" and message.message_id:
        key = intents.fingerprint(NAME, item.kind, opts.label, "mid", digest)
    else:
        key = intents.fingerprint(
            NAME, item.kind, opts.label, result.folder, str(result.uidvalidity), str(message.uid)
        )
    link = None
    if opts.link_template is not None:
        link = _link(
            opts.link_template, result.folder, str(message.uid), str(result.uidvalidity), digest
        )
        if not is_task_link(link):
            link = None
    subject = message.subject or "(no subject)"
    data: dict[str, Any] = {
        "fingerprint": key,
        "kind": item.kind,
        "rule": item.rule,
        "folder": result.folder,
        "uid": message.uid,
        "from": message.sender,
        "subject": tasks.escape_title(subject, max_length=200),
        "dates": [d.isoformat() for d in item.dates],
        "due": None if item.due is None else item.due.isoformat(),
        "received": None if message.day is None else message.day.isoformat(),
    }
    if opts.snippet_chars > 0:
        data["snippet"] = (
            tasks.escape_title(
                message.text[: opts.snippet_chars], max_length=opts.snippet_chars + 8
            )
            if message.text
            else ""
        )
    if link is not None:
        data["link"] = link
    out.items.append(data)
    if opts.emit_tasks:
        out.intents.append(
            intents.task_create(f"{item.task_prefix}{subject}", key=key, due=item.due, link=link)
        )


def build(
    connector: Connector = connect,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock = time.monotonic,
) -> Plugin:
    """The plugin. `connector` (the IMAP session), `environ` (the secrets, read at call time)
    and `clock` (the time budget) are replaceable for tests."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="New mail from IMAP folders, read-only, as requests and dates by rules.",
    )

    @plugin.source(f"{NAME}.messages")
    def messages_source(
        call: Call,
        host: str | None = None,
        state_path: str | None = None,
        port: int | None = None,
        security: str = "ssl",
        allow_plaintext: bool = False,
        allow_loopback: bool = False,
        allowed_hosts: list[str] | None = None,
        account: str = "mail",
        folders: list[str] | None = None,
        rules: list[dict[str, Any]] | None = None,
        max_messages: int = 50,
        max_body_kib: int = 8,
        baseline_messages: int = 0,
        update_state: bool = True,
        date_order: str = "dmy",
        snippet_chars: int = 160,
        emit_tasks: bool = False,
        link_template: str | None = None,
        fingerprint_by: str = "uid",
        timeout_s: int = 15,
        budget_s: int = 25,
    ) -> Output:
        """The mail that arrived since the last run in `folders` (default `INBOX`) of the IMAP
        server `host`, read-only: nothing is marked read, moved or deleted. Messages that match
        a rule in `rules` become `request` or `date` items (`emit_tasks`: also `task.create`
        intents). `state_path` is the JSON file where each folder's UIDVALIDITY and last UID are
        kept; the first run (or a changed UIDVALIDITY) starts from now and handles at most
        `baseline_messages` of the newest mail."""
        params = {k: v for k, v in locals().items() if k not in ("call", "environ", "clock")}
        opts = options(call, params)
        env = os.environ if environ is None else environ
        username, password = env.get(USERNAME_SECRET), env.get(PASSWORD_SECRET)
        if not username:
            raise PluginError(f"secret_missing: {USERNAME_SECRET} is not set")
        if not password:
            raise PluginError(f"secret_missing: {PASSWORD_SECRET} is not set")
        previous = state_file.load(opts.state_path)
        mine = dict(previous.get(opts.label, {}))
        deadline = clock() + opts.budget_s
        session = connector(opts.endpoint, username, password)
        out = Collected()
        results: list[FolderResult] = []
        try:
            for folder in opts.folders:
                left = opts.max_messages - sum(r.fetched for r in results)
                result = _read_folder(
                    session, folder, mine.get(folder), opts, left, (deadline, clock), out
                )
                results.append(result)
                mine[folder] = state_file.FolderState(result.uidvalidity, result.last_uid)
        finally:
            session.logout()
        updated = False
        if opts.update_state:
            state_file.save(opts.state_path, {**previous, opts.label: mine})
            updated = True
        return Output(
            {
                "account": opts.label,
                "folders": [r.describe() for r in results],
                "items": out.items,
                "truncated": any(r.more for r in results),
                "state_updated": updated,
            },
            intents=out.intents,
        )

    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_imap --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=HORAE,
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-imap",
    )
