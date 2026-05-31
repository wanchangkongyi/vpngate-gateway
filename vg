#!/usr/bin/env python3
"""工具函数：延迟测试、节点解析、IP 信息查询"""
from __future__ import annotations
import csv
import io
import base64
import json
import os
import re
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
}

API_URL = "https://www.vpngate.net/api/iphone/"

_ip_cache: dict[str, dict] = {}
_ip_cache_lock = threading.RLock()


def fetch_api() -> str:
    req = urllib.request.Request(
        API_URL,
        headers={"User-Agent": "Mozilla/5.0 vpngate-v2/1.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_nodes(raw: str, country_filter: list[str] | None = None) -> list[dict]:
    lines = [l for l in raw.splitlines() if not l.startswith("*")]
    if lines and lines[0].startswith("#"):
        lines[0] = lines[0][1:]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    nodes = []
    seen = set()
    for row in reader:
        ip = row.get("IP", "").strip()
        if not ip or ip in seen:
            continue
        b64 = row.get("OpenVPN_ConfigData_Base64", "").strip()
        if not b64:
            continue
        cc = row.get("CountryShort", "").strip().upper()
        if country_filter and cc not in country_filter:
            continue
        try:
            config_text = base64.b64decode(b64).decode("utf-8", errors="replace")
        except Exception:
            continue
        host, port = _parse_remote(config_text, ip)
        country_long = row.get("CountryLong", "").strip()
        nodes.append({
            "id":           f"{cc}_{ip.replace('.','_')}_{port}",
            "ip":           ip,
            "hostname":     host,
            "port":         port,
            "country":      COUNTRY_ZH.get(country_long, country_long),
            "country_code": cc,
            "score":        int(row.get("Score", 0) or 0),
            "ping":         int(row.get("Ping", 0) or 0),
            "speed":        int(row.get("Speed", 0) or 0),
            "config_text":  config_text,
            "latency_ms":   0,
            "probe_status": "not_checked",
        })
        seen.add(ip)
    return nodes


def _parse_remote(config: str, fallback_ip: str) -> tuple[str, int]:
    host, port = fallback_ip, 443
    for line in config.splitlines():
        line = line.strip()
        if line.startswith("remote "):
            parts = line.split()
            if len(parts) >= 3:
                host = parts[1]
                try:
                    port = int(parts[2])
                except ValueError:
                    pass
            break
    return host, port


def tcp_latency(host: str, port: int, timeout: float = 4) -> int:
    """TCP 握手延迟（ms），失败返回 0"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t = time.time()
        s.connect((host, port))
        ms = int((time.time() - t) * 1000)
        s.close()
        return max(1, ms)
    except Exception:
        return 0


def test_openvpn(config_text: str, timeout: int = 15) -> tuple[bool, str]:
    """
    尝试用 OpenVPN 握手验证节点可用性（--route-nopull，不改路由）
    返回 (ok, message)
    """
    import tempfile, subprocess, queue
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ovpn", delete=False) as f:
        f.write(config_text)
        tmp = f.name

    # 写临时认证文件
    auth_tmp = tmp + ".auth"
    with open(auth_tmp, "w") as f:
        f.write("vpn\nvpn\n")

    cmd = [
        "openvpn", "--config", tmp,
        "--route-nopull", "--dev", "tun99",
        "--connect-retry-max", "1", "--connect-timeout", "10",
        "--auth-user-pass", auth_tmp, "--auth-nocache",
        "--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM",
        "--verb", "3",
    ]
    ok, msg = False, "超时"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        q: queue.Queue[str | None] = queue.Queue()
        done = [False]

        def reader():
            assert proc.stdout
            for line in proc.stdout:
                if not done[0]:
                    q.put(line.rstrip())
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        started = time.time()
        while time.time() - started < timeout:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            low = line.lower()
            if "initialization sequence completed" in low:
                ok, msg = True, "握手成功"
                break
            if "auth_failed" in low:
                msg = "AUTH_FAILED"
                break
    finally:
        done[0] = True
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass
        for p in [tmp, auth_tmp]:
            try:
                os.unlink(p)
            except Exception:
                pass
    return ok, msg


def enrich_ip(nodes: list[dict]) -> None:
    """批量查询 IP 地理/ISP 信息"""
    with _ip_cache_lock:
        to_query = [n["ip"] for n in nodes
                    if n["ip"] not in _ip_cache or
                    time.time() - _ip_cache[n["ip"]].get("at", 0) > 7 * 86400]

    if not to_query:
        _apply_cache(nodes)
        return

    try:
        payload = json.dumps(to_query[:100]).encode()
        req = urllib.request.Request(
            "http://ip-api.com/batch?fields=status,query,country,city,isp,org,as,proxy,hosting",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "vpngate-v2/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        with _ip_cache_lock:
            for item in data:
                if item.get("status") == "success":
                    ip = item["query"]
                    _ip_cache[ip] = {
                        "isp":      item.get("org") or item.get("isp") or "",
                        "location": f"{item.get('country','')} {item.get('city','')}".strip(),
                        "asn":      item.get("as", ""),
                        "at":       time.time(),
                    }
    except Exception:
        pass
    _apply_cache(nodes)


def _apply_cache(nodes: list[dict]) -> None:
    with _ip_cache_lock:
        for n in nodes:
            info = _ip_cache.get(n["ip"], {})
            n["isp"]      = info.get("isp", "")
            n["location"] = info.get("location", "")
            n["asn"]      = info.get("asn", "")


def check_proxy(port: int = 7928, timeout: int = 6) -> dict:
    """通过本地代理测试出口 IP"""
    import subprocess
    cmd = [
        "curl", "-4", "-s",
        "-x", f"socks5h://127.0.0.1:{port}",
        "-w", "\n%{time_total} %{http_code}",
        "http://api.ipify.org",
        "--max-time", str(timeout)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        if res.returncode == 0:
            lines = res.stdout.strip().splitlines()
            if len(lines) >= 2:
                ip = lines[0].strip()
                parts = lines[1].strip().split()
                if len(parts) == 2 and parts[1] == "200" and ip:
                    return {"ok": True, "ip": ip, "ms": int(float(parts[0]) * 1000)}
    except Exception:
        pass
    return {"ok": False, "ip": "", "ms": 0}
