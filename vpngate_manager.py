#!/usr/bin/env python3
"""
VPNGate Gateway 核心管理器
负责：抓节点 / 测速 / OpenVPN连接 / 策略路由 / 自动切换 / 定时轮换
"""
from __future__ import annotations
import concurrent.futures
import json
import os
import queue
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
INSTALL_DIR  = Path("/opt/vpngate-gateway")
DATA_DIR     = INSTALL_DIR / "data"
NODES_FILE   = DATA_DIR / "nodes.json"
STATE_FILE   = DATA_DIR / "state.json"
CONFIG_DIR   = DATA_DIR / "configs"
AUTH_FILE    = DATA_DIR / "auth.txt"
LOG_FILE     = DATA_DIR / "vpngate.log"
SETTINGS_FILE= DATA_DIR / "settings.json"
CMD_FILE     = DATA_DIR / "cmd.json"
CMD_RESULT   = DATA_DIR / "cmd_result.json"

PROXY_HOST   = "127.0.0.1"
PROXY_PORT   = 7928
TUN_DEV      = "tun0"
ROUTE_TABLE  = 100

# Watchdog 指数退避参数
_WD_MIN_INTERVAL  = 30    # 首次重连等待（秒）
_WD_MAX_INTERVAL  = 600   # 最大等待（10 分钟）
_WD_BACKOFF_MULT  = 2.0

# ── 全局状态 ──────────────────────────────────────────────────────────────────
_lock                         = threading.RLock()
_active_process: subprocess.Popen | None = None
_active_node_id: str          = ""
_is_connecting: bool          = False
_rotate_timer: threading.Timer | None = None
_wd_fail_count: int           = 0      # watchdog 连续失败计数


# ── 持久化 ────────────────────────────────────────────────────────────────────
def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 设置 ──────────────────────────────────────────────────────────────────────
def load_settings() -> dict:
    defaults: dict = {
        "country_filter": [],    # [] = 不过滤
        "rotate_hours":   2,     # 0 = 不自动轮换
        "top_nodes":      20,
        "proxy_port":     7928,
        "probe_count":    10,    # OpenVPN 握手验证节点数
        "auto_fetch_on_start": True,
    }
    saved = _read_json(SETTINGS_FILE, {})
    defaults.update(saved)
    return defaults


def save_settings(s: dict) -> None:
    _write_json(SETTINGS_FILE, s)


# ── 节点 & 状态 ───────────────────────────────────────────────────────────────
def get_nodes() -> list[dict]:
    return _read_json(NODES_FILE, [])


def save_nodes(nodes: list[dict]) -> None:
    _write_json(NODES_FILE, nodes)


def get_state() -> dict:
    s = _read_json(STATE_FILE, {})
    s["active_node_id"] = _active_node_id
    s["is_connecting"]  = _is_connecting
    s["pid"]            = os.getpid()
    return s


def _update_state(**kw: Any) -> None:
    s = _read_json(STATE_FILE, {})
    s.update(kw)
    _write_json(STATE_FILE, s)


# ── 日志 ──────────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()

def log(msg: str) -> None:
    ts   = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            # 日志轮转：超过 5 MB 时截断
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                bak = LOG_FILE.with_suffix(".log.1")
                LOG_FILE.rename(bak)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ── 策略路由 ──────────────────────────────────────────────────────────────────
def _run(*args: str, check: bool = False) -> int:
    r = subprocess.run(list(args), capture_output=True)
    return r.returncode


def _setup_routing() -> None:
    """
    等待 tun0 就绪，然后配置策略路由：
    从 tun0 出去的流量 → 路由表 100 → 默认 via tun0
    SSH/管理流量走 eth0，不受影响。
    """
    log(f"[路由] 等待 {TUN_DEV} 就绪...")
    for i in range(30):
        r = subprocess.run(["ip", "link", "show", TUN_DEV], capture_output=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    else:
        log(f"[路由] 警告：{TUN_DEV} 30 秒内未就绪")
        return

    # 清旧规则
    _cleanup_routing()

    _run("ip", "route", "add", "default", "dev", TUN_DEV,
         "table", str(ROUTE_TABLE))
    _run("ip", "rule", "add", "oif", TUN_DEV,
         "table", str(ROUTE_TABLE), "priority", "100")
    log(f"[路由] 策略路由已配置：oif={TUN_DEV} → table {ROUTE_TABLE}")


def _cleanup_routing() -> None:
    _run("ip", "rule", "del", "table", str(ROUTE_TABLE))
    _run("ip", "route", "flush", "table", str(ROUTE_TABLE))


# ── OpenVPN ───────────────────────────────────────────────────────────────────
def _openvpn_alive() -> bool:
    return _active_process is not None and _active_process.poll() is None


def _stop_openvpn() -> None:
    global _active_process, _active_node_id
    _cleanup_routing()
    if _active_process:
        if _active_process.poll() is None:
            _active_process.terminate()
            try:
                _active_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _active_process.kill()
        _active_process = None
    # 兜底：杀掉同名进程
    subprocess.run(["pkill", "-f", r"openvpn.*vpngate-gateway"], capture_output=True)
    _active_node_id = ""


def _launch_openvpn(node: dict) -> tuple[bool, str]:
    """
    启动 OpenVPN，等待连接建立（最多 40 秒）。
    成功时设置策略路由并返回 (True, "连接成功")。
    """
    global _active_process, _active_node_id

    cfg_path = CONFIG_DIR / f"{node['id']}.ovpn"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(node["config_text"], encoding="utf-8")
    AUTH_FILE.write_text("vpn\nvpn\n", encoding="utf-8")
    AUTH_FILE.chmod(0o600)

    cmd = [
        "openvpn",
        "--config",          str(cfg_path),
        "--dev",             TUN_DEV,
        "--dev-type",        "tun",
        "--route-nopull",
        "--connect-retry-max", "1",
        "--connect-timeout", "20",
        "--auth-user-pass",  str(AUTH_FILE),
        "--auth-nocache",
        "--pull-filter", "ignore", "route-ipv6",
        "--pull-filter", "ignore", "ifconfig-ipv6",
        "--verb", "3",
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace"
    )
    q: queue.Queue[str | None] = queue.Queue()
    streaming = [True]

    def _reader() -> None:
        assert proc.stdout
        for line in proc.stdout:
            stripped = line.rstrip()
            q.put(stripped)
            if not streaming[0]:
                log(f"[VPN] {stripped}")
        q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    ok, msg = False, "超时（40s）"
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                msg = f"进程意外退出（code={proc.returncode}）"
                break
            continue
        if line is None:
            msg = "进程退出（无输出）"
            break
        low = line.lower()
        if "initialization sequence completed" in low:
            ok, msg = True, "连接成功"
            break
        if "auth_failed" in low or "authentication failed" in low:
            msg = "AUTH_FAILED"
            break
        if "connection refused" in low:
            msg = "连接被拒绝"
            break
        if "tls handshake failed" in low:
            msg = "TLS 握手失败"
            break

    streaming[0] = False   # 连接后继续把输出写到日志

    if ok:
        _active_process = proc
        _active_node_id = node["id"]
        threading.Thread(target=_setup_routing, daemon=True).start()
    else:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass

    return ok, msg


# ── 节点抓取与测试 ────────────────────────────────────────────────────────────
def fetch_and_test(
    country_filter: list[str] | None = None,
    top: int = 20,
    probe_count: int = 10,
) -> list[dict]:
    """
    完整的节点发现流程：
    1. 从 VPNGate API 抓取节点
    2. 并发 TCP 延迟测试
    3. 并发 OpenVPN 握手验证（前 probe_count 个）
    4. 查询 IP 地理信息
    5. 保存并返回
    """
    log("[抓取] 正在获取 VPNGate 节点列表...")
    _update_state(message="正在获取节点列表...", fetching=True)
    try:
        raw = vpn_utils.fetch_api()
    except RuntimeError as e:
        log(f"[抓取] 失败: {e}")
        _update_state(message=str(e), fetching=False)
        raise

    nodes = vpn_utils.parse_nodes(raw, country_filter)
    log(f"[抓取] 解析到 {len(nodes)} 个节点")
    _update_state(message=f"正在测试 {len(nodes)} 个节点延迟...")

    # 并发延迟测试
    def _do_latency(n: dict) -> dict:
        n["latency_ms"] = vpn_utils.tcp_latency(n["ip"], n["port"])
        return n

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        nodes = list(ex.map(_do_latency, nodes))

    reachable = sorted(
        [n for n in nodes if n["latency_ms"] > 0],
        key=lambda n: n["latency_ms"]
    )[:top]
    unreachable_count = len(nodes) - len([n for n in nodes if n["latency_ms"] > 0])
    log(f"[测速] 可达 {len(reachable)} 个（不可达 {unreachable_count} 个），保留前 {top} 个")
    _update_state(message=f"正在验证 {min(probe_count, len(reachable))} 个节点可用性...")

    # 并发 OpenVPN 握手验证
    check_n = min(probe_count, len(reachable))

    def _do_probe(n: dict) -> dict:
        ok, msg = vpn_utils.test_openvpn(n["config_text"])
        n["probe_status"]  = "available" if ok else "unavailable"
        n["probe_message"] = msg
        n["probed_at"]     = time.time()
        return n

    if check_n > 0:
        # 握手测试占用 openvpn 进程较多，限制并发防止资源耗尽
        workers = min(check_n, 5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            probed = list(ex.map(_do_probe, reachable[:check_n]))
        reachable[:check_n] = probed
        available = sum(1 for n in probed if n["probe_status"] == "available")
        log(f"[握手] 验证完成：{available}/{check_n} 个可用")

    # IP 地理信息
    log("[IP] 正在查询节点地理信息...")
    vpn_utils.enrich_ip(reachable)

    save_nodes(reachable)
    _update_state(
        message=f"节点更新完成，共 {len(reachable)} 个",
        fetching=False,
        fetched_at=time.time(),
    )
    log(f"[完成] 节点列表已保存，共 {len(reachable)} 个节点")
    return reachable


# ── 连接管理 ──────────────────────────────────────────────────────────────────
def connect_node(node_id: str) -> tuple[bool, str]:
    global _is_connecting, _wd_fail_count
    with _lock:
        if _is_connecting:
            return False, "正在连接中，请稍候"
        _is_connecting = True

    try:
        nodes = get_nodes()
        node  = next((n for n in nodes if n["id"] == node_id), None)
        if not node:
            return False, f"节点不存在: {node_id}"

        log(f"[连接] 开始连接: {node_id} ({node.get('country','')} {node.get('ip','')})")
        _update_state(message=f"正在连接 {node_id}...", is_connecting=True)

        # 先断开旧连接
        _stop_openvpn()

        ok, msg = _launch_openvpn(node)

        if ok:
            for n in nodes:
                n["active"] = (n["id"] == node_id)
            save_nodes(nodes)
            _update_state(
                message=f"已连接: {node_id}",
                is_connecting=False,
                connected_at=time.time(),
                connected_node=node_id,
            )
            log(f"[连接] 成功: {node_id} | 延迟 {node.get('latency_ms',0)} ms")
            _wd_fail_count = 0
            _schedule_rotate()
        else:
            _update_state(message=f"连接失败: {msg}", is_connecting=False)
            log(f"[连接] 失败: {node_id} — {msg}")

        return ok, msg
    finally:
        _is_connecting = False


def auto_connect(country: str | None = None) -> tuple[bool, str]:
    """
    自动选择最佳节点连接。
    优先选择已验证可用（probe_status=available）且延迟最低的节点。
    country: 两字母国家代码过滤（如 "JP"）
    """
    nodes = get_nodes()
    if not nodes:
        return False, "没有节点，请先执行 fetch"

    if country:
        country = country.upper()
        nodes = [n for n in nodes if n.get("country_code") == country]
        if not nodes:
            return False, f"没有 {country} 节点"

    # 按优先级排序：available > not_checked > unavailable，延迟升序
    def _priority(n: dict) -> tuple:
        s = n.get("probe_status", "not_checked")
        order = {"available": 0, "not_checked": 1, "unavailable": 2}
        return (order.get(s, 1), n.get("latency_ms") or 9999)

    candidates = [n for n in nodes if n.get("probe_status") != "unavailable"]
    if not candidates:
        candidates = nodes  # 全部不可用时不过滤
    candidates.sort(key=_priority)

    return connect_node(candidates[0]["id"])


def rotate_node() -> tuple[bool, str]:
    """切换到节点列表中的下一个节点（跳过不可用）"""
    nodes = get_nodes()
    if not nodes:
        return False, "没有节点"

    available = [
        n for n in nodes
        if n.get("probe_status") != "unavailable"
    ]
    if not available:
        available = nodes

    ids = [n["id"] for n in available]
    if _active_node_id in ids:
        idx = (ids.index(_active_node_id) + 1) % len(ids)
    else:
        idx = 0

    return connect_node(ids[idx])


def disconnect() -> None:
    global _rotate_timer
    with _lock:
        if _rotate_timer:
            _rotate_timer.cancel()
            _rotate_timer = None
    _stop_openvpn()
    nodes = get_nodes()
    for n in nodes:
        n["active"] = False
    save_nodes(nodes)
    _update_state(message="已断开连接", is_connecting=False, connected_node="")
    log("[断开] VPN 已断开")


# ── 定时轮换 ──────────────────────────────────────────────────────────────────
def _schedule_rotate() -> None:
    global _rotate_timer
    with _lock:
        if _rotate_timer:
            _rotate_timer.cancel()
        s = load_settings()
        hours = float(s.get("rotate_hours", 0))
        if hours <= 0:
            return
        _rotate_timer = threading.Timer(hours * 3600, _do_scheduled_rotate)
        _rotate_timer.daemon = True
        _rotate_timer.start()
        log(f"[轮换] 已设置 {hours:.1f} 小时后自动轮换")


def _do_scheduled_rotate() -> None:
    log("[轮换] 自动轮换节点...")
    ok, msg = rotate_node()
    log(f"[轮换] 结果: {'成功' if ok else '失败'} — {msg}")


# ── Watchdog（带指数退避） ─────────────────────────────────────────────────────
def _watchdog() -> None:
    global _wd_fail_count
    interval = _WD_MIN_INTERVAL

    while True:
        time.sleep(15)
        if not _active_node_id or _is_connecting:
            continue
        if _openvpn_alive():
            # 进程活着，重置退避
            if _wd_fail_count > 0:
                _wd_fail_count = 0
                interval = _WD_MIN_INTERVAL
            continue

        # OpenVPN 进程已挂，启动重连退避
        _wd_fail_count += 1
        wait = min(interval, _WD_MAX_INTERVAL)
        log(f"[监控] OpenVPN 进程退出（第 {_wd_fail_count} 次），{wait}s 后重连...")
        time.sleep(wait)
        interval = min(interval * _WD_BACKOFF_MULT, _WD_MAX_INTERVAL)

        if not _is_connecting:
            log("[监控] 尝试自动重连...")
            ok, msg = auto_connect()
            if ok:
                log(f"[监控] 重连成功: {msg}")
                _wd_fail_count = 0
                interval = _WD_MIN_INTERVAL
            else:
                log(f"[监控] 重连失败: {msg}")


# ── 命令总线（vg 命令通过文件传递） ──────────────────────────────────────────
def _cmd_bus() -> None:
    """
    读取 DATA_DIR/cmd.json，执行命令，结果写入 cmd_result.json。
    每 0.5 秒轮询一次，通过 ts 字段去重。
    """
    last_ts = 0.0
    while True:
        time.sleep(0.5)
        try:
            if not CMD_FILE.exists():
                continue
            cmd = _read_json(CMD_FILE)
            ts  = cmd.get("ts", 0.0)
            if ts <= last_ts:
                continue
            last_ts = ts

            action = cmd.get("cmd", "")
            args   = cmd.get("args", [])
            ok, msg = False, "未知命令"

            try:
                if action == "fetch":
                    s = load_settings()
                    nodes = fetch_and_test(
                        country_filter=args[0].split(",") if args else s.get("country_filter") or None,
                        top=int(args[1]) if len(args) > 1 else s.get("top_nodes", 20),
                        probe_count=s.get("probe_count", 10),
                    )
                    ok, msg = True, f"抓取完成，共 {len(nodes)} 个节点"
                elif action == "auto_connect":
                    country = args[0] if args else None
                    ok, msg = auto_connect(country)
                elif action == "connect" and args:
                    ok, msg = connect_node(args[0])
                elif action == "rotate":
                    ok, msg = rotate_node()
                elif action == "disconnect":
                    disconnect()
                    ok, msg = True, "已断开"
                elif action == "status":
                    ok, msg = True, json.dumps(get_state(), ensure_ascii=False)
            except Exception as e:
                msg = str(e)

            _write_json(CMD_RESULT, {
                "cmd": action, "ok": ok, "msg": msg,
                "ts": time.time(),
            })
        except Exception:
            pass


# ── 初始化 ────────────────────────────────────────────────────────────────────
def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text("vpn\nvpn\n", encoding="utf-8")
    AUTH_FILE.chmod(0o600)

    s = load_settings()
    port = s.get("proxy_port", PROXY_PORT)

    # 代理服务
    threading.Thread(
        target=proxy_server.start_proxy_server,
        args=(PROXY_HOST, port),
        daemon=True,
        name="proxy-server",
    ).start()
    log(f"[启动] 代理服务启动 {PROXY_HOST}:{port}")

    # Watchdog
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    log("[启动] Watchdog 已启动")

    # 命令总线
    threading.Thread(target=_cmd_bus, daemon=True, name="cmd-bus").start()
    log("[启动] 命令总线已启动")


# ── 主程序 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 60)
    log("[启动] VPNGate Gateway v2 启动中...")
    log("=" * 60)
    init()

    s = load_settings()
    if s.get("auto_fetch_on_start", True):
        try:
            fetch_and_test(
                country_filter=s.get("country_filter") or None,
                top=s.get("top_nodes", 20),
                probe_count=s.get("probe_count", 10),
            )
            auto_connect()
        except Exception as e:
            log(f"[启动] 初始化失败: {e}")
            log("[启动] 服务继续运行，可手动执行 vpngw fetch && vpngw auto")
    else:
        log("[启动] auto_fetch_on_start=false，跳过自动抓取")

    # 主线程保活
    while True:
        time.sleep(60)
