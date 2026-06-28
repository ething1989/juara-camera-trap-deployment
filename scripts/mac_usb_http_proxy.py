#!/usr/bin/env python3
from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
from urllib.parse import urlsplit


BUFFER = 64 * 1024


class ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        line = self.rfile.readline(BUFFER)
        if not line:
            return
        try:
            method, target, version = line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
        except ValueError:
            self._send_error(400, "Bad Request")
            return
        headers = self._read_headers()
        if method.upper() == "CONNECT":
            self._handle_connect(target, version)
            return
        self._handle_http(method, target, version, headers)

    def _read_headers(self) -> list[bytes]:
        headers: list[bytes] = []
        while True:
            line = self.rfile.readline(BUFFER)
            if not line or line in (b"\r\n", b"\n"):
                break
            headers.append(line)
        return headers

    def _handle_connect(self, target: str, version: str) -> None:
        host, port_text = _split_host_port(target, default_port=443)
        try:
            upstream = socket.create_connection((host, int(port_text)), timeout=self.timeout)
        except OSError as exc:
            self._send_error(502, f"Bad Gateway: {exc}")
            return
        with upstream:
            self.wfile.write(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
            self._relay(self.connection, upstream)

    def _handle_http(self, method: str, target: str, version: str, headers: list[bytes]) -> None:
        parsed = urlsplit(target)
        if not parsed.hostname:
            self._send_error(400, "Proxy requires absolute HTTP URLs")
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            upstream = socket.create_connection((parsed.hostname, port), timeout=self.timeout)
        except OSError as exc:
            self._send_error(502, f"Bad Gateway: {exc}")
            return
        with upstream:
            upstream.sendall(f"{method} {path} {version}\r\n".encode("iso-8859-1"))
            for header in headers:
                lower = header.lower()
                if lower.startswith(b"proxy-connection:"):
                    continue
                if lower.startswith(b"connection:"):
                    upstream.sendall(b"Connection: close\r\n")
                    continue
                upstream.sendall(header)
            upstream.sendall(b"\r\n")
            self._relay(self.connection, upstream, client_may_send=False)

    def _relay(self, client: socket.socket, upstream: socket.socket, client_may_send: bool = True) -> None:
        sockets = [client, upstream] if client_may_send else [upstream]
        while True:
            readable, _, errored = select.select(sockets, [], sockets, self.timeout)
            if errored or not readable:
                return
            for sock in readable:
                try:
                    data = sock.recv(BUFFER)
                except OSError:
                    return
                if not data:
                    return
                other = upstream if sock is client else client
                try:
                    other.sendall(data)
                except OSError:
                    return

    def _send_error(self, code: int, message: str) -> None:
        body = f"{code} {message}\n".encode("utf-8")
        self.wfile.write(
            f"HTTP/1.1 {code} {message}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode(
                "ascii", "replace"
            )
            + body
        )


class ThreadingProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _split_host_port(value: str, default_port: int) -> tuple[str, str]:
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        if rest.startswith(":"):
            return host, rest[1:]
        return host, str(default_port)
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, port
    return value, str(default_port)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tiny HTTP/HTTPS proxy for sharing Mac internet to a Pi over SSH -R.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3128)
    args = parser.parse_args()
    with ThreadingProxy((args.host, args.port), ProxyHandler) as server:
        print(f"proxy_listening={args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
