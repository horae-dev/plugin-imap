# SPDX-License-Identifier: Apache-2.0
"""`ca_file`: trust one PEM for one connection, verification never off.

The first tests build the context and check the parameter's gates. The last ones run a real
STARTTLS handshake against a throw-away in-process server whose certificate comes from a CA
generated here with the `openssl` command line (synthetic, deleted with `tmp_path`)."""

import contextlib
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
from horae_sdk import PluginError

from conftest import FakeFolder, Harness
from horae_imap.session import Endpoint, ImapSession, tls_context

OPENSSL = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(OPENSSL is None, reason="the openssl command is needed")


def make_ca(directory: Path, name: str) -> tuple[Path, Path, Path]:
    """A CA and a server certificate for 127.0.0.1 signed by it: (ca.pem, cert.pem, key.pem)."""
    assert OPENSSL
    ca_key, ca_pem = directory / f"{name}-ca.key", directory / f"{name}-ca.pem"
    key, csr, pem = directory / f"{name}.key", directory / f"{name}.csr", directory / f"{name}.pem"
    ext = directory / f"{name}.ext"
    ext.write_text("subjectAltName=IP:127.0.0.1\n", "utf-8")

    def run(*args: str) -> None:
        subprocess.run([OPENSSL, *args], check=True, capture_output=True)  # noqa: S603

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_pem),
        "-days",
        "2",
        "-subj",
        f"/CN=test-ca-{name}",
    )
    run(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        "/CN=127.0.0.1",
    )
    run(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_pem),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(pem),
        "-days",
        "2",
        "-extfile",
        str(ext),
    )
    return ca_pem, pem, key


class StarttlsServer:
    """Speaks just enough IMAP: greeting, CAPABILITY, STARTTLS, LOGIN, LOGOUT."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(10)
        self.port: int = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(10)
                with contextlib.suppress(OSError, ssl.SSLError):  # a refusing client hangs up
                    self._session(conn)

    def _session(self, conn: socket.socket) -> None:
        streams: list[socket.socket] = []
        try:
            self._talk(conn, streams)
        finally:
            for stream in streams:
                stream.close()

    def _talk(self, conn: socket.socket, streams: list[socket.socket]) -> None:
        stream: socket.socket | ssl.SSLSocket = conn
        secure = False
        stream.sendall(b"* OK [CAPABILITY IMAP4rev1 STARTTLS LOGINDISABLED] ready\r\n")
        while True:
            line = b""
            while not line.endswith(b"\r\n"):
                chunk = stream.recv(1)
                if not chunk:
                    return
                line += chunk
            tag, _, command = line.decode().strip().partition(" ")
            word = command.split(" ")[0].upper()
            if word == "CAPABILITY":
                caps = "IMAP4rev1" if secure else "IMAP4rev1 STARTTLS LOGINDISABLED"
                stream.sendall(f"* CAPABILITY {caps}\r\n{tag} OK done\r\n".encode())
            elif word == "STARTTLS":
                stream.sendall(f"{tag} OK go ahead\r\n".encode())
                stream = self.context.wrap_socket(conn, server_side=True)
                streams.append(stream)
                secure = True
            elif word == "LOGIN":
                stream.sendall(f"{tag} OK logged in\r\n".encode())
            else:
                stream.sendall(f"* BYE\r\n{tag} OK bye\r\n".encode())
                return

    def close(self) -> None:
        self.sock.close()
        self.thread.join(5)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[StarttlsServer, Path, Path]]:
    """The server, the CA that signed it and an unrelated CA."""
    ca, cert, key = make_ca(tmp_path, "good")
    other, _, _ = make_ca(tmp_path, "other")
    started = StarttlsServer(cert, key)
    yield started, ca, other
    started.close()


def endpoint(port: int, ca_file: Path | None) -> Endpoint:
    return Endpoint("127.0.0.1", port, "starttls", 5.0, None if ca_file is None else str(ca_file))


@needs_openssl
def test_the_context_trusts_only_the_given_pem_and_still_verifies(tmp_path: Path) -> None:
    ca, _, _ = make_ca(tmp_path, "good")
    context = tls_context(str(ca))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert [c["subject"] for c in context.get_ca_certs()] == [((("commonName", "test-ca-good"),),)]


def test_a_missing_or_garbage_pem_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate", "utf-8")
    with pytest.raises(ssl.SSLError):
        tls_context(str(junk))
    with pytest.raises(OSError, match="No such file"):
        tls_context(str(tmp_path / "missing.pem"))


@needs_openssl
def test_a_real_starttls_handshake_succeeds_with_the_right_ca(
    server: tuple[StarttlsServer, Path, Path],
) -> None:
    started, ca, _ = server
    ImapSession(endpoint(started.port, ca), "user", "password").logout()


@needs_openssl
def test_the_same_server_is_refused_without_ca_file(
    server: tuple[StarttlsServer, Path, Path],
) -> None:
    started, _, _ = server
    with pytest.raises(PluginError, match="tls_failed"):
        ImapSession(endpoint(started.port, None), "user", "password")


@needs_openssl
def test_another_ca_does_not_vouch_for_the_server(
    server: tuple[StarttlsServer, Path, Path],
) -> None:
    started, _, other = server
    with pytest.raises(PluginError, match="tls_failed"):
        ImapSession(endpoint(started.port, other), "user", "password")


@needs_openssl
def test_a_mismatched_host_name_is_still_refused(tmp_path: Path) -> None:
    """The certificate is for 127.0.0.1; connecting by another name must fail the host check."""
    ca, cert, key = make_ca(tmp_path, "good")
    started = StarttlsServer(cert, key)
    try:
        with pytest.raises(PluginError, match="tls_failed"):
            ImapSession(Endpoint("localhost", started.port, "starttls", 5.0, str(ca)), "u", "p")
    finally:
        started.close()


# -- the parameter ------------------------------------------------------------------------

Make = Callable[[Mapping[str, FakeFolder]], Harness]


def inbox() -> dict[str, FakeFolder]:
    return {"INBOX": FakeFolder(1000, {1: (b"Subject: a\r\n", b"x")})}


def pem_file(tmp_path: Path) -> Path:
    ca, _, _ = make_ca(tmp_path, "param")
    return ca


@needs_openssl
def test_ca_file_reaches_the_endpoint_when_the_lock_allows_reading(
    harness: Make, tmp_path: Path
) -> None:
    h: Harness = harness(inbox())
    ca = pem_file(tmp_path)
    result = h.run(security="starttls", ca_file=str(ca), paths=[h.root, str(tmp_path)])
    assert result.error is None
    assert h.endpoints[0][0].ca_file == str(ca.resolve())


@needs_openssl
def test_ca_file_needs_a_path_approved_by_the_lock(harness: Make, tmp_path: Path) -> None:
    h: Harness = harness(inbox())
    ca = pem_file(tmp_path)
    result = h.run(security="starttls", ca_file=str(ca), paths=[h.root])
    assert result.error is not None
    assert "path_not_approved" in result.error
    assert h.endpoints == []


def test_ca_file_must_be_an_absolute_pem(harness: Make, tmp_path: Path) -> None:
    h: Harness = harness(inbox())
    junk = tmp_path / "junk.pem"
    junk.write_text("nope", "utf-8")
    for value, word in (("relative.pem", "path_not_approved"), (str(junk), "config_invalid")):
        result = h.run(security="starttls", ca_file=value, paths=[h.root, str(tmp_path)])
        assert result.error is not None
        assert word in result.error
    assert h.endpoints == []


def test_ca_file_makes_no_sense_in_plain_text(harness: Make) -> None:
    h: Harness = harness(inbox())
    result = h.run(security="plain", allow_plaintext=True, ca_file="/x/ca.pem")
    assert result.error is not None
    assert "config_invalid" in result.error
