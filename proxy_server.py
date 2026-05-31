#!/usr/bin/env python3
"""
HTTP/SOCKS5 代理服务器
所有出站连接通过 SO_BINDTODEVICE 强制绑定 tun0
VPN 断开时直接返回 502，不回落到真实 IP
"""
from __future__ import annotations
import select
import socket
import threading
import urllib.parse
import time
from typing import Any

BIND_DEVICE = b"tun0"

def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("连接断开")
        data += chunk
    return data

def create_connection(host: str, port: int, timeout: float = 20) -> socket.socket:
    """创建出站连接，强制绑定 tun0"""
    # 先尝试解析 IP
    try:
        socket.inet_aton(host)
        resolved = host
    except OSError:
        resolved = None

    if not resolved:
        # 通过 tun0 做 DNS 解析
        resolved = _resolve_via_tun0(host) or host

    err = None
    for res in socket.getaddrinfo(resolved, port, socket.AF_INET, socket.SOCK_STREAM):
        af, socktype, proto, _, sa = res
        s = None
        try:
            s = socket.socket(af, socktype, proto)
            s.settimeout(timeout)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, BIND_DEVICE)
            s.connect(sa)
            return s
        except OSError as e:
            err = e
            if s:
                s.close()
    raise err or OSError("连接失败")

def _resolve_via_tun0(host: str, dns: str = "8.8.8.8", timeout: float = 3.0) -> str | None:
    """通过 tun0 发送 DNS 查询"""
    import random
    tx_id = random.getrandbits(16).to_bytes(2, "big")
    flags = b"\x01\x00"
    qname = b""
    for part in host.split("."):
        if part:
            encoded = part.encode("idna")
            qname += len(encoded).to_bytes(1, "big") + encoded
    qname += b"\x00"
    packet = tx_id + flags + b"\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, BIND_DEVICE)
        s.sendto(packet, (dns, 53))
        resp, _ = s.recvfrom(2048)
    except Exception:
        return None
    finally:
        s.close()

    if len(resp) < 12 or resp[:2] != tx_id or (resp[3] & 0x0F) != 0:
        return None

    offset = 12
    # 跳过问题区
    while offset < len(resp):
        l = resp[offset]
        if l == 0:
            offset += 5
            break
        elif (l & 0xC0) == 0xC0:
            offset += 6
            break
        else:
            offset += 1 + l

    answers = int.from_bytes(resp[6:8], "big")
    for _ in range(answers):
        if offset >= len(resp):
            break
        while offset < len(resp):
            l = resp[offset]
            if l == 0:
                offset += 1
                break
            elif (l & 0xC0) == 0xC0:
                offset += 2
                break
            else:
                offset += 1 + l
        if offset + 10 > len(resp):
            break
        atype = int.from_bytes(resp[offset:offset+2], "big")
        rdlen = int.from_bytes(resp[offset+8:offset+10], "big")
        offset += 10
        if atype == 1 and rdlen == 4:
            return socket.inet_ntoa(resp[offset:offset+4])
        offset += rdlen
    return None

def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, errored = select.select(sockets, [], sockets, 120)
        if errored:
            return
        for src in readable:
            dst = right if src is left else left
            data = src.recv(65536)
            if not data:
                return
            dst.sendall(data)

def handle_socks5(client: socket.socket) -> None:
    upstream = None
    try:
        n = recv_exact(client, 1)[0]
        recv_exact(client, n)
        client.sendall(b"\x05\x00")
        ver, cmd, _, atype = recv_exact(client, 4)
        if ver != 5 or cmd != 1:
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return
        if atype == 1:
            host = socket.inet_ntoa(recv_exact(client, 4))
        elif atype == 3:
            host = recv_exact(client, recv_exact(client, 1)[0]).decode("idna")
        elif atype == 4:
            host = socket.inet_ntop(socket.AF_INET6, recv_exact(client, 16))
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return
        port = int.from_bytes(recv_exact(client, 2), "big")
        try:
            upstream = create_connection(host, port)
        except Exception:
            client.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            return
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        relay(client, upstream)
    finally:
        client.close()
        if upstream:
            upstream.close()

def handle_http(client: socket.socket, first: bytes) -> None:
    upstream = None
    try:
        data = first
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, port_s = target.partition(":")
            upstream = create_connection(host, int(port_s) or 443)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            relay(client, upstream)
            return
        parsed = urllib.parse.urlsplit(target)
        port = parsed.port or 80
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = [l for l in lines[1:] if not l.lower().startswith(("proxy-connection:", "connection:"))]
        req = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\nConnection: close\r\n\r\n"
        upstream = create_connection(parsed.hostname, port)
        upstream.sendall(req.encode("iso-8859-1") + rest)
        relay(client, upstream)
    except Exception:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    finally:
        client.close()
        if upstream:
            upstream.close()

def handle_client(client: socket.socket) -> None:
    try:
        client.settimeout(30)
        first = recv_exact(client, 1)
        if first == b"\x05":
            handle_socks5(client)
        else:
            handle_http(client, first)
    except Exception:
        try:
            client.close()
        except OSError:
            pass

def start_proxy_server(host: str = "127.0.0.1", port: int = 7928) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(256)
    print(f"[代理] HTTP/SOCKS5 监听 {host}:{port}", flush=True)
    while True:
        try:
            client, _ = server.accept()
            threading.Thread(target=handle_client, args=(client,), daemon=True).start()
        except Exception as e:
            print(f"[代理] accept 错误: {e}", flush=True)
            time.sleep(0.5)
