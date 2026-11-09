# Proton Mail through Proton Mail Bridge

Proton has no IMAP server. Proton Mail Bridge is a local program that logs in to Proton and serves your mail over IMAP on your own machine. `plugin-imap` reads it like any other IMAP server, read-only. This page is the recipe, and it ends with a manual end-to-end check (the maintainer's acceptance step for [#3](https://github.com/horae-dev/plugin-imap/issues/3)).

What to know first:

- Bridge needs a paid Proton plan.
- The Bridge login is **not** your Proton password: Bridge makes its own IMAP username and password (below).
- Bridge serves IMAP with **STARTTLS** on port **1143** (SMTP on 1025) and a **self-signed certificate** it generated itself.
- Everything below was written from the projects' documentation. The command names and file paths for Bridge and the community image can change between versions: `help` inside the CLI is the truth for your version.

## 1. Run Bridge headless

Two ways. Both keep Bridge's state (login, keys, certificate) in a persistent volume.

### a. The official Bridge in CLI mode

Install Proton's own Bridge package for your system (<https://proton.me/mail/bridge>) and start it without a window:

```sh
protonmail-bridge --cli        # also accepted: -c
```

Proton documents the `-c` flag and the `help` command ([Bridge CLI guide](https://proton.me/support/bridge-cli-guide)). On a server you run it as a service under your init system, with the same user every time.

### b. A community Docker image

Proton publishes **no official Docker image** (as far as we could find when this was written). The usual one is [`shenxn/protonmail-bridge`](https://hub.docker.com/r/shenxn/protonmail-bridge) (source: <https://github.com/shenxn/protonmail-bridge-docker>). **It is a third-party, unofficial image.** It wraps the official Bridge (its `deb` tags use Proton's release package, its `build` tags build the source), but you are trusting its maintainer with your mailbox credentials. Read its Dockerfile and decide.

Pin it by digest, never by a moving tag. Tag `3.19.0-1` resolved to the digest below on Docker Hub when this page was written (its last push was in April 2025, so check for newer tags; Bridge versions older than a few months may stop logging in). Re-check with `docker buildx imagetools inspect shenxn/protonmail-bridge:<tag>` and update the digest, which is the image you reviewed:

```
shenxn/protonmail-bridge@sha256:3717b4441130675dc9131196de9f9c5287d2ea21b138d83b0486429e1737638a
```

## 2. Log in once, interactively

Bridge asks for your Proton login (and 2FA) in its own shell, once. It then keeps the session in its volume.

Official Bridge: `protonmail-bridge --cli`, then `login` and follow the prompts.

Community image, with the same volume you will use to run it:

```sh
docker run --rm -it -v protonmail:/root \
  shenxn/protonmail-bridge@sha256:3717b4441130675dc9131196de9f9c5287d2ea21b138d83b0486429e1737638a init
```

At the `>>>` prompt: `login`, then your Proton address, password and 2FA code.

## 3. Get the Bridge's IMAP username and password

In the same Bridge shell run:

```
info
```

It prints, per account, the **username** (usually your Proton address) and a **generated password** for mail clients, plus host `127.0.0.1`, IMAP port `1143` (STARTTLS) and SMTP port `1025`. Use those two values, not your Proton account password, as the plugin's secrets:

```
IMAP_USERNAME=<the username Bridge printed>
IMAP_PASSWORD=<the generated Bridge password>
```

Put them in Horae's secret store or the plugin's environment like any secret. Never commit them or paste them in an issue. If you lose the password, run `info` again in the Bridge shell (the community image's `init` command opens the same shell). Changing it is done in Bridge.

## 4. The certificate (STARTTLS on 1143)

Bridge's certificate is self-signed, so the system trust store rejects it and the plugin stops with `tls_failed`. The plugin never turns verification off, and there is no switch for it. Two safe ways to make the certificate trusted:

### Option 1 (recommended): `ca_file`

Since 0.1.2 `imap.messages` takes `ca_file`, an absolute path to a PEM file. Then:

- that connection trusts **only** the certificates in that file (the system store is not used for it: it is a pin), and the chain and the host name are still checked;
- nothing global changes, and the file is never used for another connection or another plugin;
- the file is read only if the lock approves it: add its directory to `paths.read` for this plugin in `horae.lock` (the same way as the `state_path` directory is approved for writing). Without that, the call fails with `path_not_approved` before any network use;
- a file that is not a PEM certificate fails with `config_invalid`, and `ca_file` with `security: plain` is refused.

Export Bridge's certificate as a PEM. It is the file `cert.pem` in Bridge's config directory (typically `~/.config/protonmail/bridge-v3/cert.pem`, or under `/root/.config/protonmail/bridge-v3/` in the community image's volume; run `help` in the Bridge CLI for a `cert` command in your version). Copy it **to a directory only Horae can read**, for example `/etc/horae/certs/proton-bridge.pem`, and do not copy the matching `key.pem`.

Check what the certificate names, because the host name check stays on:

```sh
openssl x509 -in /etc/horae/certs/proton-bridge.pem -noout -subject -ext subjectAltName -enddate
```

The host you configure (`127.0.0.1`) must be listed there as an IP address. If your Bridge version only lists a DNS name, or the file's names do not cover the address you connect to, the plugin will refuse the connection and that is correct; fix it on the Bridge side (for example by importing a certificate of your own that names the address) rather than looking for a way around the check. A Bridge update can regenerate its certificate: when `tls_failed` appears after an update, export it again.

### Option 2: trust it system-wide

Add the same PEM to the operating system's trust store (Debian and Ubuntu: copy it to `/usr/local/share/ca-certificates/proton-bridge.crt`, then `sudo update-ca-certificates`). No `ca_file` is needed afterwards, but **every** program on that machine then trusts that certificate. Prefer Option 1.

## 5. The pipeline

```yaml
# horae.yaml (a source entry; the exact surrounding keys depend on your Horae version)
- source: imap.messages
  with:
    account: proton
    host: 127.0.0.1
    port: 1143
    security: starttls
    ca_file: /etc/horae/certs/proton-bridge.pem
    state_path: /var/lib/horae/imap/proton-state.json
    folders:
      - INBOX
      - Folders/School        # your own folders live under "Folders/"
      - Labels/Invoices       # labels live under "Labels/"
    rules:
      - name: school-requests
        kind: request
        from: ["*@school.example.org"]
```

Bridge exposes Proton folders as `INBOX`, `Folders/<name>` and `Labels/<name>`, plus system folders such as `Sent`, `Drafts`, `Archive`, `Spam`, `Trash` and `All Mail`. Write them exactly as Bridge lists them. A label is a view: the same message can appear in a folder and in a label, and the plugin deduplicates nothing across folders, so use either, not both, for the same rule. Your names may differ (accents, other languages): run the check in section 8 to list them.

The lock (`horae.lock`) approves, for this plugin:

```yaml
permissions:
  network_local:
    - 127.0.0.1:1143
  paths:
    read:
      - /etc/horae/certs
    write:
      - /var/lib/horae/imap
  secrets:
    - IMAP_USERNAME
    - IMAP_PASSWORD
```

`network_local` is checked by the plugin as `call.address_allowed(host, port)` against the exact `host:port`: a grant of `127.0.0.1:1143` approves exactly that. A core older than horae-sdk 0.6 sends no list and needs `allow_loopback: true` on the entry instead.

## 6. Bridge and Horae with Docker Compose (Coolify)

Hostnames below are generic placeholders. Bridge's ports are not published on the host: only Horae reaches them.

```yaml
services:
  proton-bridge:
    image: shenxn/protonmail-bridge@sha256:3717b4441130675dc9131196de9f9c5287d2ea21b138d83b0486429e1737638a
    restart: unless-stopped
    volumes:
      - protonmail:/root          # the login, keys and certificate (back it up; it is a secret)
    networks:
      internal:
        aliases:
          - proton-bridge.internal
    # no "ports:": nothing is published on the host

  horae:
    image: registry.example.org/horae:pinned-tag-or-digest
    restart: unless-stopped
    depends_on: [proton-bridge]
    environment:
      IMAP_USERNAME_FILE: /run/secrets/imap_username   # however your Horae reads secrets
    volumes:
      - horae-data:/var/lib/horae
      - ./certs:/etc/horae/certs:ro   # holds proton-bridge.pem only
    networks: [internal]

networks:
  internal:
    internal: true     # no route out; Horae's own egress is a separate network if it needs one

volumes:
  protonmail:
  horae-data:
```

Run the login step once with `docker compose run --rm proton-bridge init` (same volume), then `docker compose up -d`. In Coolify, create the two services in one stack so they share the generated network, and mount the volume and the certificate directory from the stack's storage.

### Which host and grant to use

Decision 66 in the core (`schemas/README.md`) says what `network_local` accepts: an IP in a private range or a name ending in `.local`, `.lan`, `.internal` or `.home.arpa`, always with an explicit port; never `localhost` and never a single-label name such as `proton-bridge`. So:

- **Same host or same network namespace as Bridge** (Bridge installed natively, or the Horae container started with `network_mode: "service:proton-bridge"`): use `host: 127.0.0.1`, `port: 1143` and the grant `127.0.0.1:1143`. This is what `plugin.yaml` declares today. It is the simplest, and the certificate (checked against `127.0.0.1`) is the one Bridge made for that address.
- **Separate containers on a compose network, as above**: the plugin connects to the alias, so the grant must name the alias with the port Bridge listens on there: `proton-bridge.internal:<port>`. A bare service name (`proton-bridge`) is refused by the grant syntax, which is why the compose file adds the `.internal` alias. The community image listens on **143** inside the container (it relays to Bridge's 1143 on its own loopback), so the grant and the `port` there are `proton-bridge.internal:143`. Two things are needed that are **not** done by this recipe: the plugin's `plugin.yaml` declares only `127.0.0.1:1143` (a lock grant for another endpoint needs the manifest to declare it, a permission change to review separately), and the certificate must name `proton-bridge.internal`, which Bridge's own certificate normally does not. With the plugin's checks kept on, this variant therefore only works once both are solved.

Until then, use the loopback form. The two variants differ only in `host`, `port` and the grant.

## 7. Limits and security notes

- The plugin reads only: no flags are changed (`EXAMINE`, `BODY.PEEK`).
- The Bridge password gives full IMAP and SMTP access to the mailbox. Keep it in the secret store; rotate it by re-adding the account in Bridge.
- Bridge syncs the mailbox to disk on first login, which can take a while and some space. The first plugin run after a login may find folders still empty.
- A Bridge update that re-generates its certificate, or a re-login, can change the UIDVALIDITY: the plugin then re-baselines without flooding.

## 8. Manual end-to-end check (maintainer, real Proton account)

1. Start Bridge and log in once (section 2). `info` shows the username and password.
2. Test the port: `openssl s_client -starttls imap -connect 127.0.0.1:1143 -showcerts </dev/null` shows the certificate; save the leaf as `proton-bridge.pem` and check its names with the `openssl x509` command of section 4.
3. Put `IMAP_USERNAME` and `IMAP_PASSWORD` in Horae's secrets and approve the lock (section 5).
4. Run the entry with `update_state: false` (a dry run), `folders: [INBOX]` and a rule that matches a mail you know is there. Expect `state_updated: false` and your item.
5. Negative checks: remove `ca_file` and expect `tls_failed`; point `ca_file` at a directory the lock does not approve and expect `path_not_approved`; remove the `network_local` grant and expect `host_not_approved`.
6. Run once more with `update_state: true`, then confirm in a mail client (or in Proton's web app) that nothing was marked read, moved or deleted.
7. List the exact folder and label names: `openssl s_client -starttls imap -connect 127.0.0.1:1143 -quiet`, then `a LOGIN <user> <password>` and `b LIST "" "*"`.
