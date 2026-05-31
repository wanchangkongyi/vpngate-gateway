#!/usr/bin/env python3
"""
HTTP/SOCKS5 双协议代理服务器
所有出站连接通过 SO_BINDTODEVICE 强制绑定 tun0
VPN 断开（tun0 不存在）时直接返回 502，不回落到真实 IP
"""
from __future__ import annotations
import select
import socket
import struct
import threading
import time
import urllib.parse
import os
import logging
from typing import Optional

log = logging.getLogger("proxy")

BIND_DEVICE   = b"tun0"
CONNECT_TIMEOUT = 20.0
RELAY_TIMEOUT   = 300.0   # 5 分钟无数据后断开


# ── tun0 存在检查 ─────────────────────────────────────────────────────────────
def _tun0_exists() -> bool:
    """检查 tun0 是否存在且 UP"""
    try:
        with open("/proc/net/if_inet6") as _:
            pass
    except Exception:
        pass
    try:
        # /sys/class/net/tun0/operstate
        p = f"/sys/class/net/{BIND_DEVICE.decode()}/operstate"
        return os.path.exists(p)
    except Exception:
        return False


# ── 工具 ──────────────────────────────────────────────────────────────────────
def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("连接提前关闭")
        data += chunk
    return data


def _create_upstream(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """
    创建出站 TCP 连接，强制绑定 tun0。
    tun0 不存在时抛出 OSError（上层转为 502）。
    """
    if not _tun0_exists():
        raise OSError("tun0 未就绪，VPN 未连接")

    # 优先用系统解析器，回退到直接连接
    addrs = []
    try:
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

    last_err: Exception = OSError("无可用地址")
    for af, socktype, proto, _, sa in addrs:
        s: Optional[socket.socket] = None
        try:
            s = socket.socket(af, socktype, proto)
            s.settimeout(timeout)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, BIND_DEVICE)
            s.connect(sa)
            s.settimeout(None)   # 进入 relay 后用 select，不需要超时
            return s
        except OSError as e:
            last_err = e
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    raise last_err


# ── 双向中继 ──────────────────────────────────────────────────────────────────
def _relay(left: socket.socket, right: socket.socket) -> None:
    """
    双向透传，任一端关闭或超时则退出。
    """
    sockets = [left, right]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, RELAY_TIMEOUT)
            if errored or not readable:
                return
            for src in readable:
                dst = right if src is left else left
                try:
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
                except OSError:
                    return
    except Exception:
        pass


# ── SOCKS5 ────────────────────────────────────────────────────────────────────
def _handle_socks5(client: socket.socket) -> None:
    upstream: Optional[socket.socket] = None
    try:
        # 握手
        n_methods = _recv_exact(client, 1)[0]
        _recv_exact(client, n_methods)            # 忽略认证方法列表
        client.sendall(b"\x05\x00")               # 无认证

        # 请求
        header = _recv_exact(client, 4)
        ver, cmd, _, atype = header
        if ver != 5:
            return
        if cmd != 1:                              # 只支持 CONNECT
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return

        if atype == 0x01:                         # IPv4
            host = socket.inet_ntoa(_recv_exact(client, 4))
        elif atype == 0x03:                       # 域名
            dlen = _recv_exact(client, 1)[0]
            host = _recv_exact(client, dlen).decode("utf-8", errors="replace")
        elif atype == 0x04:                       # IPv6
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return

        port = struct.unpack("!H", _recv_exact(client, 2))[0]

        try:
            upstream = _create_upstream(host, port)
        except OSError as e:
            # tun0 不可用或连接失败 → Host unreachable (0x04)
            log.warning(f"SOCKS5 上游连接失败 {host}:{port} — {e}")
            client.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            return

        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        _relay(client, upstream)
    except Exception as e:
        log.debug(f"SOCKS5 处理异常: {e}")
    finally:
        _safe_close(client)
        _safe_close(upstream)


# ── HTTP/HTTPS ────────────────────────────────────────────────────────────────
def _handle_http(client: socket.socket, first_byte: bytes) -> None:
    upstream: Optional[socket.socket] = None
    try:
        # 读完整请求头
        buf = first_byte
        client.settimeout(10)
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
        client.settimeout(None)

        head, _, body = buf.partition(b"\r\n\r\n")
        try:
            head_str = head.decode("iso-8859-1")
        except Exception:
            return
        lines = head_str.split("\r\n")
        if not lines:
            return

        parts = lines[0].split(" ", 2)
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]

        if method.upper() == "CONNECT":
            # HTTPS 隧道
            host, _, port_s = target.rpartition(":")
            port = int(port_s) if port_s.isdigit() else 443
            try:
                upstream = _create_upstream(host, port)
            except OSError as e:
                log.warning(f"CONNECT 上游失败 {target} — {e}")
                _send_ignore(client, b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            _send_ignore(client, b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if body:
                _send_ignore(upstream, body)
            _relay(client, upstream)
            return

        # 普通 HTTP 代理
        parsed = urllib.parse.urlsplit(target)
        if not parsed.hostname:
            return
        port   = parsed.port or 80
        path   = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        # 过滤 Proxy- 头
        fwd_headers = [
            l for l in lines[1:]
            if l and not l.lower().startswith(("proxy-connection:", "proxy-authorization:"))
        ]
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            + "\r\n".join(fwd_headers)
            + "\r\nConnection: close\r\n\r\n"
        )
        try:
            upstream = _create_upstream(parsed.hostname, port)
        except OSError as e:
            log.warning(f"HTTP 上游失败 {parsed.hostname}:{port} — {e}")
            _send_ignore(client, b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        upstream.sendall(req.encode("iso-8859-1") + body)
        _relay(client, upstream)

    except Exception as e:
        log.debug(f"HTTP 处理异常: {e}")
        _send_ignore(client, b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
    finally:
        _safe_close(client)
        _safe_close(upstream)


# ── 分派 ──────────────────────────────────────────────────────────────────────
def _handle_client(client: socket.socket) -> None:
    try:
        client.settimeout(15)
        first = _recv_exact(client, 1)
        client.settimeout(None)
        if first == b"\x05":
            _handle_socks5(client)
        else:
            _handle_http(client, first)
    except Exception as e:
        log.debug(f"客户端分派异常: {e}")
        _safe_close(client)


def _safe_close(s: Optional[socket.socket]) -> None:
    if s is None:
        return
    try:
        s.close()
    except Exception:
        pass


def _send_ignore(s: socket.socket, data: bytes) -> None:
    try:
        s.sendall(data)
    except Exception:
        pass


# ── 入口 ──────────────────────────────────────────────────────────────────────
def start_proxy_server(
    host: str = "127.0.0.1",
    port: int = 7928,
    backlog: int = 256,
) -> None:
    """启动代理服务器（阻塞）"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(backlog)
    print(f"[代理] HTTP/SOCKS5 监听 {host}:{port}", flush=True)
    while True:
        try:
            client, addr = server.accept()
            threading.Thread(
                target=_handle_client,
                args=(client,),
                daemon=True,
                name=f"proxy-{addr[0]}:{addr[1]}",
            ).start()
        except Exception as e:
            log.error(f"[代理] accept 异常: {e}")
            time.sleep(0.5)
