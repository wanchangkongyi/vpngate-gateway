#!/usr/bin/env python3
"""
工具函数：延迟测试、节点解析、IP 信息查询
"""
from __future__ import annotations
import csv
import io
import base64
import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

# ── 国家名翻译 ────────────────────────────────────────────────────────────────
COUNTRY_ZH: dict[str, str] = {
    "Japan": "日本", "Korea Republic of": "韩国", "Thailand": "泰国",
    "United States": "美国", "United Kingdom": "英国", "Russian Federation": "俄罗斯",
    "Viet Nam": "越南", "Vietnam": "越南", "China": "中国", "Taiwan": "台湾",
    "Taiwan Province of China": "台湾", "Hong Kong": "香港", "Singapore": "新加坡",
    "Malaysia": "马来西亚", "Indonesia": "印度尼西亚", "India": "印度",
    "Philippines": "菲律宾", "Australia": "澳大利亚", "Canada": "加拿大",
    "France": "法国", "Germany": "德国", "Netherlands": "荷兰",
    "Sweden": "瑞典", "Brazil": "巴西", "Ukraine": "乌克兰",
    "Mongolia": "蒙古", "Cambodia": "柬埔寨", "Laos": "老挝",
    "Myanmar": "缅甸", "Bangladesh": "孟加拉国", "Sri Lanka": "斯里兰卡",
    "Nepal": "尼泊尔", "Pakistan": "巴基斯坦", "Turkey": "土耳其",
    "Mexico": "墨西哥", "Argentina": "阿根廷", "Chile": "智利",
    "Colombia": "哥伦比亚", "Italy": "意大利", "Spain": "西班牙",
    "Poland": "波兰", "Czech Republic": "捷克", "Romania": "罗马尼亚",
    "South Africa": "南非", "Egypt": "埃及", "Nigeria": "尼日利亚",
    "Israel": "以色列", "Saudi Arabia": "沙特阿拉伯", "United Arab Emirates": "阿联酋",
}

API_URL      = "https://www.vpngate.net/api/iphone/"
API_MIRROR   = "https://www.vpngate.net/api/iphone/"   # 可替换为镜像

_ip_cache: dict[str, dict] = {}
_ip_cache_lock = threading.RLock()


# ── API 抓取 ──────────────────────────────────────────────────────────────────
def fetch_api(timeout: int = 20, retries: int = 3) -> str:
    """抓取 VPNGate API，带重试"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API_URL,
                headers={"User-Agent": "Mozilla/5.0 vpngate-gateway/2.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 指数退避
    raise RuntimeError(f"API 抓取失败（已重试 {retries} 次）: {last_err}")


# ── 节点解析 ──────────────────────────────────────────────────────────────────
def parse_nodes(raw: str, country_filter: list[str] | None = None) -> list[dict]:
    """解析 VPNGate CSV，返回节点列表"""
    lines = [l for l in raw.splitlines() if not l.startswith("*")]
    if lines and lines[0].startswith("#"):
        lines[0] = lines[0][1:]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    nodes: list[dict] = []
    seen: set[str] = set()

    for row in reader:
        ip = row.get("IP", "").strip()
        if not ip or ip in seen:
            continue
        b64 = row.get("OpenVPN_ConfigData_Base64", "").strip()
        if not b64:
            continue
        cc = row.get("CountryShort", "").strip().upper()
        if country_filter and cc not in [c.upper() for c in country_filter]:
            continue
        try:
            config_text = base64.b64decode(b64).decode("utf-8", errors="replace")
        except Exception:
            continue

        # 修正 config：确保有 auth-retry 和 tls 设置
        config_text = _sanitize_config(config_text)
        host, port, proto = _parse_remote(config_text, ip)
        country_long = row.get("CountryLong", "").strip()

        nodes.append({
            "id":            f"{cc}_{ip.replace('.','_')}_{port}",
            "ip":            ip,
            "hostname":      host,
            "port":          port,
            "proto":         proto,
            "country":       COUNTRY_ZH.get(country_long, country_long or cc),
            "country_code":  cc,
            "score":         _safe_int(row.get("Score")),
            "ping":          _safe_int(row.get("Ping")),
            "speed":         _safe_int(row.get("Speed")),
            "uptime_days":   _safe_int(row.get("Uptime")) // 86400,
            "sessions":      _safe_int(row.get("NumVpnSessions")),
            "operator":      row.get("Operator", "").strip()[:40],
            "config_text":   config_text,
            "latency_ms":    0,
            "probe_status":  "not_checked",
            "probe_message": "",
            "probed_at":     0.0,
            "isp":           "",
            "location":      "",
            "asn":           "",
            "active":        False,
        })
        seen.add(ip)
    return nodes


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val or default)
    except (ValueError, TypeError):
        return default


def _sanitize_config(config: str) -> str:
    """移除已知不兼容指令，注入稳定性参数"""
    remove_prefixes = (
        "redirect-gateway", "dhcp-option", "route-ipv6",
        "ifconfig-ipv6", "tun-ipv6",
    )
    lines = []
    has_auth_retry = False
    has_cipher = False
    for line in config.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(p) for p in remove_prefixes):
            continue
        if stripped.startswith("auth-retry"):
            has_auth_retry = True
        if stripped.startswith("cipher") or stripped.startswith("data-ciphers"):
            has_cipher = True
        lines.append(line)
    if not has_auth_retry:
        lines.append("auth-retry nointeract")
    if not has_cipher:
        lines.append("data-ciphers AES-128-CBC:AES-256-GCM:AES-128-GCM")
    lines.append("connect-retry-max 1")
    lines.append("resolv-retry 5")
    lines.append("tls-noverify")
    return "\n".join(lines)


def _parse_remote(config: str, fallback_ip: str) -> tuple[str, int, str]:
    """从 OpenVPN config 解析 remote host/port/proto"""
    host, port, proto = fallback_ip, 443, "tcp"
    for line in config.splitlines():
        line = line.strip()
        if line.startswith("remote "):
            parts = line.split()
            if len(parts) >= 2:
                host = parts[1]
            if len(parts) >= 3:
                try:
                    port = int(parts[2])
                except ValueError:
                    pass
        if line.startswith("proto "):
            p = line.split()[1].lower()
            proto = "udp" if "udp" in p else "tcp"
    return host, port, proto


# ── 延迟测试 ──────────────────────────────────────────────────────────────────
def tcp_latency(host: str, port: int, timeout: float = 4.0) -> int:
    """TCP 握手延迟（ms）。失败返回 0，超时返回 0。"""
    try:
        # 使用系统 DNS 解析（比自制 DNS 更可靠）
        addrs = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not addrs:
            return 0
        _, _, _, _, sa = addrs[0]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.connect(sa)
        ms = int((time.monotonic() - t0) * 1000)
        s.close()
        return max(1, ms)
    except Exception:
        return 0


# ── OpenVPN 握手验证 ──────────────────────────────────────────────────────────
def test_openvpn(config_text: str, timeout: int = 18) -> tuple[bool, str]:
    """
    快速 OpenVPN 握手验证（--route-nopull，不改系统路由）
    tun 设备用随机名，避免与主连接冲突
    返回 (ok, message)
    """
    import tempfile, random, string
    suffix = "".join(random.choices(string.ascii_lowercase, k=4))
    tun_dev = f"tun9{suffix}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".ovpn", delete=False) as f:
        f.write(config_text)
        tmp_cfg = f.name

    tmp_auth = tmp_cfg + ".auth"
    with open(tmp_auth, "w") as f:
        f.write("vpn\nvpn\n")

    cmd = [
        "openvpn",
        "--config", tmp_cfg,
        "--route-nopull",
        "--dev", tun_dev,
        "--dev-type", "tun",
        "--connect-retry-max", "1",
        "--connect-timeout", "12",
        "--auth-user-pass", tmp_auth,
        "--auth-nocache",
        "--verb", "3",
        "--log", "/dev/null",
    ]
    ok, msg = False, "超时"
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"
        )
        q: queue.Queue[str | None] = queue.Queue()

        def _reader():
            assert proc and proc.stdout
            for line in proc.stdout:
                q.put(line.rstrip())
            q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    msg = "进程退出"
                    break
                continue
            if line is None:
                break
            low = line.lower()
            if "initialization sequence completed" in low:
                ok, msg = True, "握手成功"
                break
            if "auth_failed" in low or "authentication failed" in low:
                msg = "AUTH_FAILED（节点需要凭证）"
                break
            if "connection refused" in low or "connection timed out" in low:
                msg = "连接被拒绝/超时"
                break
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for p in (tmp_cfg, tmp_auth):
            try:
                os.unlink(p)
            except Exception:
                pass
    return ok, msg


# ── IP 信息查询 ───────────────────────────────────────────────────────────────
def enrich_ip(nodes: list[dict], cache_ttl: int = 7 * 86400) -> None:
    """批量查询 IP 地理/ISP 信息（ip-api.com 批量接口，每批≤100）"""
    now = time.time()
    with _ip_cache_lock:
        to_query = [
            n["ip"] for n in nodes
            if n["ip"] not in _ip_cache
            or now - _ip_cache[n["ip"]].get("at", 0) > cache_ttl
        ]

    if not to_query:
        _apply_ip_cache(nodes)
        return

    # 分批查询（每批 100 个）
    for i in range(0, len(to_query), 100):
        batch = to_query[i:i + 100]
        try:
            payload = json.dumps(batch).encode()
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=status,query,country,city,isp,org,as",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "vpngate-gateway/2.0",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data: list[dict] = json.loads(r.read())
            with _ip_cache_lock:
                for item in data:
                    if item.get("status") == "success":
                        _ip_cache[item["query"]] = {
                            "isp":      item.get("org") or item.get("isp") or "",
                            "location": f"{item.get('country','')} {item.get('city','')}".strip(),
                            "asn":      item.get("as", ""),
                            "at":       now,
                        }
        except Exception:
            pass

    _apply_ip_cache(nodes)


def _apply_ip_cache(nodes: list[dict]) -> None:
    with _ip_cache_lock:
        for n in nodes:
            info = _ip_cache.get(n["ip"], {})
            n["isp"]      = info.get("isp", "")
            n["location"] = info.get("location", "")
            n["asn"]      = info.get("asn", "")


# ── 出口 IP 检测 ──────────────────────────────────────────────────────────────
def check_proxy(port: int = 7928, timeout: int = 8) -> dict:
    """
    通过本地 SOCKS5 代理检测当前出口 IP。
    返回 {"ok": bool, "ip": str, "ms": int}
    """
    cmd = [
        "curl", "-4", "-s",
        "--socks5-hostname", f"127.0.0.1:{port}",
        "-w", r"\n%{time_total} %{http_code}",
        "https://api.ipify.org",
        "--max-time", str(timeout),
        "--connect-timeout", "5",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        if res.returncode == 0:
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                ip_line = lines[0].strip()
                meta    = lines[1].strip().split()
                if len(meta) == 2 and meta[1] == "200" and ip_line:
                    return {
                        "ok": True,
                        "ip": ip_line,
                        "ms": int(float(meta[0]) * 1000),
                    }
    except Exception:
        pass
    return {"ok": False, "ip": "", "ms": 0}
