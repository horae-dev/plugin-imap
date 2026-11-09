# Changelog

## 0.1.2

- `ca_file` (new parameter): an absolute path to a PEM file trusted for that one connection,
  for Proton Mail Bridge's self-signed STARTTLS certificate. Only that file is trusted (not the
  system store), the chain and host name are still verified, and there is no switch to turn
  verification off. The path must be approved for reading in the lock (`paths.read`, checked with
  `call.paths.allowed`); it is validated before any connection, and refused with `security: plain`.
- `docs/proton-bridge.md`: the Proton Mail Bridge recipe (headless Bridge, first login, the
  Bridge credentials, the certificate, a pipeline entry, a Compose/Coolify snippet and the manual
  end-to-end check).
- **Permissions:** none new in `plugin.yaml`. Per instance, the lock approves the `ca_file`
  directory as a `paths.read` entry.

## 0.1.1

- A local Bridge is approved through `permissions.network_local` (horae-sdk 0.6, decision 66):
  `plugin.yaml` declares `127.0.0.1:1143`, and the plugin checks
  `call.address_allowed(host, port)` before connecting. `True` connects, `False` refuses
  (`host_not_approved`), `None` (an older core) keeps the `allow_loopback` opt-in. This replaces
  the 0.1.0 behaviour where a core that sent a list always refused a loopback host.
- Requires `horae-sdk>=0.6,<0.7`.
- **Permissions (new, to approve):** network_local `127.0.0.1:1143`.

## 0.1.0

First release: the generic IMAP source.

- `imap.messages`: the mail that arrived since the last run in chosen folders, read-only
  (`EXAMINE`, `BODY.PEEK` only; no `STORE`, `EXPUNGE`, `COPY` or `MOVE` exists).
- Since-state per account and folder (UIDVALIDITY and last UID) in a JSON file at `state_path`;
  a first run or a changed UIDVALIDITY re-baselines without a flood (`baseline_messages`).
- Caps: messages per run, header and text bytes per message, a time budget.
- Rules-first extraction, no LLM: sender, subject and List-Id globs, `request` and `date` items,
  French and English dates, optional `task.create` with a stable fingerprint and a link only from
  `link_template`.
- IMAPS by default (certificate verified), STARTTLS, and plain text only with `allow_plaintext`.
- **Permissions (new, to approve):** network `imap.gmail.com`, `imap.fastmail.com`; secrets
  `IMAP_USERNAME`, `IMAP_PASSWORD`; intent `task.create`. The state file's directory is granted
  per instance as a `paths.write` entry in `horae.lock`.
