#!/usr/bin/env python3
"""
VPNGate Gateway 核心管理器
负责：抓节点 / 测速 / OpenVPN连接 / 策略路由 / 自动切换 / 定时轮换
"""
from __future__ import annotations
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import vpn_utils
import proxy_server

# ── 配置 ──────────────────────────────────────────────────────────────────────
DATA_DIR     = Path("/opt/vpngate-v2/data")
NODES_FILE   = DATA_DIR / "nodes.json"
STATE_FILE   = DATA_DIR / "state.json"
CONFIG_DIR   = DATA_DIR / "configs"
AUTH_FILE    = DATA_DIR / "auth.txt"
LOG_FILE     = DATA_DIR / "vpngate.log"
SETTINGS_FILE= DATA_DIR / "settings.json"

PROXY_HOST   = "127.0.0.1"
PROXY_PORT   = 7928
TUN_DEV      = "tun0"
ROUTE_TABLE  = 100

# ── 全局状态 ──────────────────────────────────────────────────────────────────
_lock                    = threading.RLock()
_active_process: subprocess.Popen | None = None
_active_node_id: str     = ""
_is_connecting: bool     = False
_rotate_timer: threading.Timer | None = None

# ── 持久化 ────────────────────────────────────────────────────────────────────
def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def load_settings() -> dict:
    defaults = {
        "country_filter": [],      # [] = 不过滤
        "rotate_hours":   2,       # 0 = 不自动轮换
        "top_nodes":      20,      # 保留前N个节点
        "proxy_port":     7928,
    }
    s = _read_json(SETTINGS_FILE, {})
    defaults.update(s)
    return defaults

def save_settings(s: dict) -> None:
    _write_json(SETTINGS_FILE, s)

def get_nodes() -> list[dict]:
    return _read_json(NODES_FILE, [])

def save_nodes(nodes: list[dict]) -> None:
    _write_json(NODES_FILE, nodes)

def get_state() -> dict:
    s = _read_json(STATE_FILE, {})
    s["active_node_id"] = _active_node_id
    s["is_connecting"]  = _is_connecting
    return s

def set_state(**kw) -> None:
    s = _read_json(STATE_FILE, {})
    s.update(kw)
    _write_json(STATE_FILE, s)

# ── 路由管理 ──────────────────────────────────────────────────────────────────
def _setup_routing() -> None:
    """策略路由：出接口 tun0 的流量走表100"""
    subprocess.run(["ip", "rule", "del", "table", str(ROUTE_TABLE)],
                   capture_output=True)
    subprocess.run(["ip", "route", "flush", "table", str(ROUTE_TABLE)],
                   capture_output=True)
    # 等 tun0 起来
    for _ in range(20):
        r = subprocess.run(["ip", "link", "show", TUN_DEV], capture_output=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    subprocess.run(["ip", "route", "add", "default", "dev", TUN_DEV,
                    "table", str(ROUTE_TABLE)], capture_output=True)
    subprocess.run(["ip", "rule", "add", "oif", TUN_DEV,
                    "table", str(ROUTE_TABLE)], capture_output=True)
    log(f"[路由] 策略路由已配置 oif={TUN_DEV} → table {ROUTE_TABLE}")

def _cleanup_routing() -> None:
    subprocess.run(["ip", "rule", "del", "table", str(ROUTE_TABLE)],
                   capture_output=True)
    subprocess.run(["ip", "route", "flush", "table", str(ROUTE_TABLE)],
                   capture_output=True)

# ── OpenVPN ───────────────────────────────────────────────────────────────────
def _stop_openvpn() -> None:
    global _active_process, _active_node_id
    _cleanup_routing()
    if _active_process and _active_process.poll() is None:
        _active_process.terminate()
        try:
            _active_process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _active_process.kill()
    _active_process = None
    _active_node_id = ""
    subprocess.run(["pkill", "-f", "openvpn.*vpngate-v2"], capture_output=True)

def _openvpn_running() -> bool:
    return _active_process is not None and _active_process.poll() is None

def _connect(node: dict) -> tuple[bool, str]:
    """启动 OpenVPN 连接，等待初始化完成，返回 (ok, message)"""
    global _active_process, _active_node_id, _is_connecting

    # 写配置文件
    cfg_path = CONFIG_DIR / f"{node['id']}.ovpn"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(node["config_text"], encoding="utf-8")

    cmd = [
        "openvpn", "--config", str(cfg_path),
        "--dev", TUN_DEV, "--dev-type", "tun",
        "--route-nopull",
        "--connect-retry-max", "1", "--connect-timeout", "15",
        "--auth-user-pass", str(AUTH_FILE), "--auth-nocache",
        "--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM",
        "--pull-filter", "ignore", "route-ipv6",
        "--pull-filter", "ignore", "ifconfig-ipv6",
        "--verb", "3",
    ]

    import queue
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    q: queue.Queue[str | None] = queue.Queue()
    done = [False]

    def reader():
        assert proc.stdout
        for line in proc.stdout:
            if not done[0]:
                q.put(line.rstrip())
            else:
                log(f"[VPN] {line.rstrip()}")
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    ok, msg = False, "超时"
    started = time.time()
    while time.time() - started < 35:
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
            ok, msg = True, "连接成功"
            break
        if "auth_failed" in low:
            msg = "认证失败"
            break

    done[0] = True
    if ok:
        _active_process = proc
        _active_node_id = node["id"]
        _setup_routing()
    else:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass
        try:
            cfg_path.unlink()
        except Exception:
            pass

    return ok, msg

# ── 节点抓取与测试 ────────────────────────────────────────────────────────────
def fetch_and_test(country_filter: list[str] | None = None,
                   top: int = 20) -> list[dict]:
    """抓取节点 → 测延迟 → 测 OpenVPN 握手 → 排序"""
    log("[抓取] 正在获取 VPNGate 节点列表...")
    set_state(message="正在获取节点列表...")
    raw = vpn_utils.fetch_api()
    nodes = vpn_utils.parse_nodes(raw, country_filter)
    log(f"[抓取] 获取到 {len(nodes)} 个节点")

    # 并发测延迟
    log("[测速] 正在并发测试延迟...")
    set_state(message=f"正在测试 {len(nodes)} 个节点延迟...")

    def _test_latency(n: dict) -> dict:
        n["latency_ms"] = vpn_utils.tcp_latency(n["ip"], n["port"])
        return n

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        nodes = list(ex.map(_test_latency, nodes))

    # 过滤不可达，按延迟排序
    nodes = [n for n in nodes if n["latency_ms"] > 0]
    nodes.sort(key=lambda n: n["latency_ms"])
    nodes = nodes[:top]
    log(f"[测速] 延迟测试完成，保留 {len(nodes)} 个节点")

    # 并发测 OpenVPN 握手（前10个）
    log("[握手] 正在验证节点可用性...")
    set_state(message="正在验证节点可用性...")

    def _test_ovpn(n: dict) -> dict:
        ok, msg = vpn_utils.test_openvpn(n["config_text"], timeout=15)
        n["probe_status"]  = "available" if ok else "unavailable"
        n["probe_message"] = msg
        n["probed_at"]     = time.time()
        return n

    check_count = min(10, len(nodes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=check_count) as ex:
        tested = list(ex.map(_test_ovpn, nodes[:check_count]))
    nodes[:check_count] = tested

    # 查询 IP 信息
    vpn_utils.enrich_ip(nodes)

    save_nodes(nodes)
    log(f"[完成] 节点列表已更新，共 {len(nodes)} 个节点")
    set_state(message=f"节点列表已更新，共 {len(nodes)} 个节点", fetched_at=time.time())
    return nodes

# ── 连接管理 ──────────────────────────────────────────────────────────────────
def connect_node(node_id: str) -> tuple[bool, str]:
    global _is_connecting
    with _lock:
        if _is_connecting:
            return False, "正在连接中，请稍候"
        _is_connecting = True

    try:
        nodes = get_nodes()
        node = next((n for n in nodes if n["id"] == node_id), None)
        if not node:
            return False, f"节点不存在: {node_id}"

        set_state(message=f"正在连接 {node_id}...", is_connecting=True)
        log(f"[连接] 开始连接节点: {node_id}")

        _stop_openvpn()
        ok, msg = _connect(node)

        if ok:
            # 更新节点状态
            for n in nodes:
                n["active"] = (n["id"] == node_id)
            save_nodes(nodes)
            set_state(message=f"已连接: {node_id}", is_connecting=False,
                      connected_at=time.time())
            log(f"[连接] 成功: {node_id}")
            # 启动定时轮换
            _schedule_rotate()
        else:
            set_state(message=f"连接失败: {msg}", is_connecting=False)
            log(f"[连接] 失败: {node_id} - {msg}")

        return ok, msg
    finally:
        _is_connecting = False

def auto_connect() -> tuple[bool, str]:
    """自动连接延迟最低的可用节点"""
    nodes = get_nodes()
    candidates = [n for n in nodes if n.get("probe_status") == "available"]
    if not candidates:
        candidates = [n for n in nodes if n.get("probe_status") != "unavailable"]
    if not candidates:
        return False, "没有可用节点，请先抓取"
    candidates.sort(key=lambda n: n.get("latency_ms") or 9999)
    return connect_node(candidates[0]["id"])

def rotate_node() -> tuple[bool, str]:
    """轮换到下一个节点"""
    nodes = get_nodes()
    if not nodes:
        return False, "没有节点"
    current = _active_node_id
    ids = [n["id"] for n in nodes]
    if current in ids:
        idx = (ids.index(current) + 1) % len(ids)
    else:
        idx = 0
    return connect_node(ids[idx])

def disconnect() -> None:
    global _rotate_timer
    if _rotate_timer:
        _rotate_timer.cancel()
        _rotate_timer = None
    _stop_openvpn()
    nodes = get_nodes()
    for n in nodes:
        n["active"] = False
    save_nodes(nodes)
    set_state(message="已断开连接", is_connecting=False)
    log("[断开] 已断开 VPN 连接")

# ── 定时轮换 ──────────────────────────────────────────────────────────────────
def _schedule_rotate() -> None:
    global _rotate_timer
    if _rotate_timer:
        _rotate_timer.cancel()
    s = load_settings()
    hours = s.get("rotate_hours", 0)
    if hours <= 0:
        return
    _rotate_timer = threading.Timer(hours * 3600, _do_rotate)
    _rotate_timer.daemon = True
    _rotate_timer.start()
    log(f"[轮换] 已设置 {hours} 小时后自动轮换")

def _do_rotate() -> None:
    log("[轮换] 开始自动轮换节点...")
    ok, msg = rotate_node()
    log(f"[轮换] 结果: {msg}")

# ── 日志 ──────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── 初始化 ────────────────────────────────────────────────────────────────────
def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text("vpn\nvpn\n", encoding="utf-8")
    AUTH_FILE.chmod(0o600)
    # 启动代理服务
    s = load_settings()
    threading.Thread(
        target=proxy_server.start_proxy_server,
        args=(PROXY_HOST, s.get("proxy_port", PROXY_PORT)),
        daemon=True
    ).start()
    log("[启动] 代理服务已启动")
    # 监控 OpenVPN 进程
    threading.Thread(target=_watchdog, daemon=True).start()

def _watchdog() -> None:
    """监控 OpenVPN 进程，意外退出时自动切换"""
    while True:
        time.sleep(15)
        if _active_node_id and not _openvpn_running() and not _is_connecting:
            log("[监控] OpenVPN 进程意外退出，尝试自动切换...")
            auto_connect()


if __name__ == "__main__":
    init()
    # 启动后自动抓取并连接
    s = load_settings()
    try:
        fetch_and_test(
            country_filter=s.get("country_filter") or None,
            top=s.get("top_nodes", 20)
        )
        auto_connect()
    except Exception as e:
        log(f"[启动] 初始化失败: {e}")

    # 保持主线程运行
    while True:
        time.sleep(60)
