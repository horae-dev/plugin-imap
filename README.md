<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-imap

Horae plugin: a generic, **read-only** IMAP source. It reads the mail that arrived since the last run in the folders you choose, applies your rules (sender, subject, list-id; no LLM) and returns `request` and `date` items, optionally as `task.create` intents. Proton Mail through Proton Mail Bridge is the same source pointed at `127.0.0.1` (the recipe is [docs/proton-bridge.md](docs/proton-bridge.md)).

Version 0.1.2 · requires `horae-sdk` 0.6 · Apache-2.0

## What it never does

- **It never marks mail as read, or changes the mailbox in any way.** Folders are opened with `EXAMINE` (read-only), never `SELECT`. Messages are fetched with `BODY.PEEK[...]` only, never `BODY[...]` or `RFC822`, which set `\Seen`. There is no `STORE`, `EXPUNGE`, `COPY`, `MOVE`, `APPEND` or `DELETE` anywhere in the package: every command goes through an allow-list (`CAPABILITY`, `STARTTLS`, `LOGIN`, `EXAMINE`, `UID SEARCH`, `UID FETCH`, `LOGOUT`). A test runs the plugin against a real server and compares every flag before and after; another records every command sent.
- It never follows a link in a mail, never fetches anything a mail points to, and never copies a raw body into its output: only an escaped, capped snippet.
- It never sends the password over an unverified or unencrypted connection unless you set `allow_plaintext`.

## The tool

`imap.messages` (a source). Parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `host` | required | the IMAP server: a lowercase DNS name, declared in `plugin.yaml` or listed in `allowed_hosts` **and** approved in `horae.lock`; or `localhost`, `127.0.0.1`, `::1` for a bridge on this machine, which needs an explicit `port` and a `network_local` grant (`127.0.0.1:1143`) in `horae.lock` (an older core: `allow_loopback: true`) |
| `state_path` | required | absolute path of the JSON file that keeps the since-state; its directory must be approved for writing in `horae.lock` |
| `port` | 993 (`ssl`), 143 otherwise | |
| `security` | `ssl` | `ssl` (implicit TLS), `starttls`, or `plain` (needs `allow_plaintext: true`) |
| `allow_plaintext` | `false` | the explicit opt-in for `security: plain`: **the password then travels in clear text**. Only for a loopback bridge or a network you trust completely |
| `allow_loopback` | `false` | the opt-in to connect to a loopback host (`localhost`, `127.0.0.1`, `::1`) on the configured `port` (it must be given), **only for a core older than horae-sdk 0.6** that sends no approved list. A core that sends one decides by `call.address_allowed(host, port)` against `permissions.network_local`: a granted `host:port` is approved (the flag is not needed), anything else is **refused** without connecting |
| `ca_file` | none | an absolute path to a PEM file that vouches for the server (Proton Mail Bridge's self-signed certificate): for this connection only, **only** that file is trusted instead of the system store, and the chain and host name are still verified; there is no way to turn verification off. The path must be approved for reading (`paths.read`) in `horae.lock`; needs `security` `ssl` or `starttls`. See [docs/proton-bridge.md](docs/proton-bridge.md) |
| `allowed_hosts` | none | extra server names, at most 16 (see "Other hosts") |
| `account` | `mail` | a label (A-Z a-z 0-9 . _ -, at most 32): the state key and part of the fingerprints |
| `folders` | `["INBOX"]` | 1 to 8 folder names, exactly as the server spells them (`Lists/School`, `Envoyés`) |
| `rules` | none | the rules below. Without any, nothing matches |
| `max_messages` | 50 | at most this many messages read per run, all folders together (1 to 200) |
| `max_body_kib` | 8 | per message: the header fields (at most 16 KiB) plus the first N KiB of the text (1 to 64) |
| `baseline_messages` | 0 | on a first run or a changed UIDVALIDITY, still handle this many of the newest messages (0 to `max_messages`) |
| `update_state` | `true` | `false` reads without moving the state (a dry run) |
| `date_order` | `dmy` | `dmy` or `mdy`, for `03/04/2026` |
| `snippet_chars` | 160 | characters of body text returned, escaped; 0 returns none (0 to 500) |
| `emit_tasks` | `false` | also propose a `task.create` per item |
| `link_template` | none | an `https` URL with `{folder}`, `{uid}`, `{uidvalidity}` or `{message_id_hash}`; the only source of a task link |
| `fingerprint_by` | `uid` | `uid` (folder + UIDVALIDITY + UID) or `message_id` (a hash of the Message-ID; falls back to `uid` when absent) |
| `timeout_s` | 15 | per network operation (1 to 60) |
| `budget_s` | 25 | the whole call: when it is spent the run stops cleanly and the next continues (1 to 300) |

Secrets, read at call time and never logged or returned: `IMAP_USERNAME` and `IMAP_PASSWORD`.

### Output

```json
{
  "account": "mail",
  "folders": [{"folder": "INBOX", "uidvalidity": 4242, "last_uid": 4, "fetched": 4, "matched": 2,
               "baselined": false, "uidvalidity_changed": false, "more": false}],
  "items": [
    {"fingerprint": "imap:request:mail:INBOX:4242:2", "kind": "request", "rule": "school",
     "folder": "INBOX", "uid": 2, "from": "secretariat@school.example.org",
     "subject": "Autorisation de sortie", "dates": ["2026-11-15"], "due": "2026-11-15",
     "received": "2026-11-02", "snippet": "Bonjour, merci de retourner le coupon avant le 15 novembre 2"}
  ],
  "truncated": false,
  "state_updated": true
}
```

`from`, `subject` and `snippet` come from the mail: **untrusted**. `subject` and `snippet` are escaped (`horae_sdk.tasks.escape_title`: Tasks signifiers, Markdown, wikilinks and bare links neutralised, control characters dropped); `from` is the bare address, lower case. `truncated` means a cap or the time budget stopped the run and more mail is waiting.

## Rules

Deterministic, no LLM. A rule names a `kind` and at least one matcher; the first rule that matches a message wins, so a message yields at most one item.

```yaml
rules:
  - name: school-requests          # 1 to 40 of A-Z a-z 0-9 . _ -
    kind: request                  # request | date
    from: ["*@school.example.org"] # the address, never the display name
    subject: ["*autorisation*", "*inscription*", "*please reply*"]
    task_prefix: "Reply: "         # optional, put before the subject in the task title
  - name: school-news
    kind: date
    list_id: ["news.school.example.org"]
```

- Matchers are lists of case-insensitive `fnmatch` globs (`*`, `?`, `[...]`), not regular expressions, on the sender's address, the subject and the `List-Id` (up to 300 characters of each). A field matches when any of its patterns does; a rule matches when every field it names does.
- **`request`**: the message asks for something. An item always exists; its `due` is the first date found in the subject and text that is not before the message's own day.
- **`date`**: the message announces a date. An item exists only when a date is found.
- **Dates** are extracted conservatively, in French and English: `2026-11-15`; `15/11/2026`, `15.11.2026`, `15-11-2026` (day first, or month first with `date_order: mdy`; a four-digit year is required); `15 novembre 2026`, `1er déc. 2026`, `15 November 2026`, `November 15, 2026`, `Nov 15th 2026`. A date with a month name but no year (`le 15 novembre`) is the first such day on or after the message's own `Date` header, never resolved against the clock. Impossible days, two-digit years, `demain`, `next Friday` are ignored. Only the subject and the `text/plain` start are searched (HTML-only mail has no text).

## Since-state

Per account label and folder: the UIDVALIDITY and the highest UID handled, in the JSON file at `state_path`. A run searches UIDs above the last one, in windows of 2000, so a large mailbox is never listed whole.

- **First run, or a folder with no state:** nothing is fetched and nothing is returned, unless you set `baseline_messages` (then only that many of the newest messages are handled). The state starts from the newest UID. This avoids a flood of old mail turning into tasks.
- **UIDVALIDITY changed:** the server renumbered the folder, so the old UIDs mean nothing. The folder is re-baselined exactly like a first run (`uidvalidity_changed: true`), capped by `baseline_messages`. UID-based fingerprints change with it; use `fingerprint_by: message_id` to keep task identities across a renumbering.
- **Caps and time:** when `max_messages` or `budget_s` stops a run, the state stops at the last message handled and `truncated` is true; the next run continues there.
- The state is written once, atomically, after every folder was read. A run that raises writes nothing, so its mail is read again next time. A mail whose task the core later drops is not retried: the state moves with the read, not with the apply. Use `update_state: false` for a dry run.
- The state file holds two integers per folder, no mail and no secret. Do not edit it by hand; delete it to re-baseline.

## Tasks and links

`emit_tasks: true` adds a `task.create` per item: title `task_prefix + subject` (escaped by `intents.task_create`), `due` from the dates, and a stable fingerprint `imap:<kind>:<account>:<folder>:<uidvalidity>:<uid>` (or `imap:<kind>:<account>:mid:<hash>`).

A link is added **only** when `link_template` is set, built from the template and the folder, UID and UIDVALIDITY, never from anything in the mail. As in plugin-github, the core routes a task that has a link only to a `mode: tasks` entry whose trusted `link_prefix` covers it, or the step fails with `intent_unrouted`:

```yaml
steps:
  - collect:
      - imap.messages:
          host: imap.fastmail.com
          state_path: /var/lib/horae/imap-state.json
          rules: [...]
          emit_tasks: true
          link_template: "https://app.fastmail.com/mail/{folder}"
  - act:
      - tasks:
          mode: tasks
          link_prefix: https://app.fastmail.com/
```

## Configuration examples

Pass the secrets through the instance's secret store (`IMAP_USERNAME`, `IMAP_PASSWORD`), never in a pipeline file.

### Gmail (app password)

Turn on 2-Step Verification, create an [app password](https://myaccount.google.com/apppasswords), and use it as `IMAP_PASSWORD` (your address as `IMAP_USERNAME`). IMAP must be enabled in Gmail's settings. `imap.gmail.com` is already declared.

```yaml
- imap.messages:
    host: imap.gmail.com
    state_path: /var/lib/horae/imap-gmail.json
    account: gmail
    folders: ["INBOX"]
    rules:
      - {name: school, kind: request, from: ["*@school.example.org"]}
```

### Fastmail

Create an app password with *Mail (IMAP)* access in Settings, Privacy and Security. `imap.fastmail.com` is already declared.

```yaml
- imap.messages:
    host: imap.fastmail.com
    state_path: /var/lib/horae/imap-fastmail.json
    account: fastmail
    folders: ["INBOX", "Lists/School"]
    max_messages: 100
    rules:
      - {name: forms, kind: request, subject: ["*autorisation*", "*form*"], task_prefix: "Reply: "}
      - {name: school-news, kind: date, list_id: ["news.school.example.org"]}
    emit_tasks: true
```

### Any other server

Declare the host for the instance (approve it in `horae.lock`) and list it in `allowed_hosts`:

```yaml
- imap.messages:
    host: mail.example.org
    allowed_hosts: [mail.example.org]
    security: starttls          # or ssl on 993 (the default)
    state_path: /var/lib/horae/imap-home.json
    account: home
    rules:
      - {name: bills, kind: date, from: ["billing@*"], subject: ["*invoice*", "*facture*"]}
```

### Proton Mail Bridge (loopback)

Bridge listens on `127.0.0.1` (IMAP 1143 with STARTTLS by default) and gives its own username and password. `plugin.yaml` declares `permissions.network_local: ["127.0.0.1:1143"]` (horae-sdk 0.6, decision 66); approve that entry in `horae.lock`, and use the same `host:port` in the pipeline. The plugin asks `call.address_allowed(host, port)` before connecting: `True` connects, `False` (no matching grant, or another host or port) refuses with `host_not_approved`, and `None` (a core older than 0.6, which sends no list) falls back to the explicit `allow_loopback: true` opt-in. The `port` is always required. A Bridge on another port needs its own `network_local` entry (for example `127.0.0.1:1144`) in the lock and in the plugin instance's approvals.

Lock entry:

```yaml
permissions:
  network_local:
    - 127.0.0.1:1143
```

Pipeline step:

```yaml
- imap.messages:
    host: 127.0.0.1
    port: 1143
    security: starttls
    state_path: /var/lib/horae/imap-proton.json
    account: proton
```

Bridge's certificate is self-signed and the plugin never relaxes verification, so STARTTLS to Bridge fails with `tls_failed` until its certificate is trusted by the system store of the machine that runs Horae. `security: plain` with `allow_plaintext: true` is the explicit, last-resort alternative for a bridge on this machine only. The full recipe is [plugin-imap#3](../../issues/3).

### Other hosts

`plugin.yaml` declares `imap.gmail.com` and `imap.fastmail.com`. Any other server must be (1) added to the instance's lock approvals for this plugin, and (2) listed in the call's `allowed_hosts`. Both are required: the core's list (`Call.host_allowed`) cannot be widened by a pipeline, and a pipeline cannot reach a host the lock does not hold. With an older core that sends no list, only (2) applies. IP literals are never accepted (except loopback, see `network_local`), and `localhost` is never an `allowed_hosts` value.

## Permissions

| Kind | Entry | Why |
|---|---|---|
| network | `imap.gmail.com` | Gmail IMAP |
| network | `imap.fastmail.com` | Fastmail IMAP |
| secret | `IMAP_USERNAME` | the login |
| secret | `IMAP_PASSWORD` | the password or app password |
| intent | `task.create` | only with `emit_tasks` |
| paths | none declared | the state file's directory is granted per instance in `horae.lock` (`paths.write`), checked with `call.paths.allowed(..., write=True)` |

There is no LLM call, and no network access other than the IMAP connection.

## Failures

Every expected failure raises `PluginError` with a fixed code and no server text: `config_invalid`, `secret_missing`, `host_not_approved`, `path_not_approved`, `connect_failed`, `tls_failed` (certificate or handshake), `starttls_unsupported`, `login_disabled`, `login_failed`, `folder_unavailable`, `protocol_error`, `timeout`, `state_invalid`, `state_unreadable`, `state_unwritable`. A failing folder fails the whole call and nothing is written (the core then marks the step degraded and skips `act` steps). A corrupt state file refuses instead of silently re-baselining.

## Development and tests

```
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov            # 85% gate
uv run python -m horae_imap --manifest > plugin.yaml   # after changing tools or permissions
```

The integration tests (`tests/test_greenmail.py`) run against a real IMAP server, [GreenMail](https://greenmail-mail-test.github.io/greenmail/) 2.1.15, a service container in CI pinned by digest. They skip locally unless you start one (they fail, never skip, when `CI` is set):

```
docker run -d --rm --name greenmail -p 3143:3143 -p 3993:3993 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.imap -Dgreenmail.setup.test.imaps -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=tester:test-password@example.org' \
  greenmail/standalone@sha256:309ba544ea482e5d4265d09921cdab07c8dad2cc4481d715c9b8ff5d3f0a149f
HORAE_IMAP_TEST_HOST=127.0.0.1 HORAE_IMAP_TEST_PORT=3143 HORAE_IMAP_TEST_IMAPS_PORT=3993 \
HORAE_IMAP_TEST_USER=tester HORAE_IMAP_TEST_PASSWORD=test-password uv run pytest
```

GreenMail has no STARTTLS and a self-signed IMAPS certificate: the tests prove `starttls_unsupported` and that the certificate is refused; the happy path runs over its plain port on loopback with `allow_plaintext` and `allow_loopback`. Fixtures and recordings are synthetic (`example.org`).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
