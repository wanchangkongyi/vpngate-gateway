#!/bin/bash
# VPNGate Gateway 一键安装脚本
# 基于 OpenVPN + 策略路由，将 VPNGate 节点作为 VPS 出口 IP
# 本地暴露 SOCKS5/HTTP 代理供 Xray/3x-ui 使用

set -e

# ── 颜色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${CYAN}══ $* ${NC}"; }

# ── 检查 root ─────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "请用 root 权限运行: sudo bash install.sh"

# ── 检查系统 ──────────────────────────────────────────────────────────────────
source /etc/os-release 2>/dev/null || true
[[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *"ubuntu"* ]] && \
    warn "当前系统非 Ubuntu，可能存在兼容性问题"

INSTALL_DIR="/opt/vpngate-gateway"
SERVICE_NAME="vpngate-gateway"
PROXY_PORT=7928
WEB_PORT=8787

step "安装依赖"
apt-get update -qq
apt-get install -y -qq \
    openvpn curl python3 python3-pip \
    iproute2 iptables net-tools dnsutils \
    jq bc

# microsocks 用于 SOCKS5 代理
if ! command -v microsocks &>/dev/null; then
    info "编译安装 microsocks..."
    apt-get install -y -qq gcc make
    cd /tmp
    curl -sL https://github.com/rofl0r/microsocks/archive/refs/heads/master.tar.gz | tar xz
    cd microsocks-master && make && make install
    cd /
fi

step "创建目录结构"
mkdir -p "$INSTALL_DIR"/{bin,ovpn,logs}

step "安装核心脚本"

# ── 节点抓取脚本 ──────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/fetch_nodes.py" << 'PYEOF'
#!/usr/bin/env python3
"""抓取 VPNGate 节点，按延迟/速度排序，输出 ovpn 配置文件"""

import csv, io, base64, os, sys, json, re, socket, time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import URLError

API_URL   = "https://www.vpngate.net/api/iphone/"
OVPN_DIR  = "/opt/vpngate-gateway/ovpn"
NODE_FILE = "/opt/vpngate-gateway/nodes.json"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; vpngate-gw/1.0)"}

def fetch_raw():
    req = Request(API_URL, headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")

def extract_port(b64):
    try:
        cfg = base64.b64decode(b64).decode("utf-8", errors="replace")
        m = re.search(r'^remote\s+\S+\s+(\d+)', cfg, re.MULTILINE)
        return int(m.group(1)) if m else 443
    except:
        return 443

def ping_host(ip, port=443, timeout=3):
    """TCP 握手测延迟（毫秒），失败返回 9999"""
    try:
        start = time.time()
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return int((time.time() - start) * 1000)
    except:
        return 9999

def parse_nodes(raw, country_filter=None):
    lines = [l for l in raw.splitlines() if not l.startswith("*")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    nodes = []
    for row in reader:
        cc = row.get("CountryShort","").strip().upper()
        if country_filter and cc not in country_filter:
            continue
        hostname = row.get("#HostName","").strip()
        ip       = row.get("IP","").strip()
        b64      = row.get("OpenVPN_ConfigData_Base64","").strip()
        if not hostname or not ip or not b64:
            continue
        if "." not in hostname:
            hostname += ".opengw.net"
        port  = extract_port(b64)
        speed = int(row.get("Speed", 0) or 0)
        nodes.append({
            "hostname": hostname,
            "ip":       ip,
            "port":     port,
            "country":  cc,
            "speed":    speed,
            "b64":      b64,
            "ping":     9999,
        })
    return nodes

def test_nodes(nodes, workers=30):
    """并发测延迟"""
    def _test(node):
        node["ping"] = ping_host(node["ip"], node["port"])
        return node
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_test, n): n for n in nodes}
        results = []
        for f in as_completed(futures):
            results.append(f.result())
    return results

def save_ovpn(node):
    """保存 ovpn 配置到文件"""
    cfg = base64.b64decode(node["b64"]).decode("utf-8", errors="replace")
    # 修改 remote 行使用主机名
    cfg = re.sub(
        r'^(remote\s+)\S+(\s+\d+)',
        lambda m: m.group(1) + node["hostname"] + m.group(2).rstrip(),
        cfg, flags=re.MULTILINE
    )
    # 追加认证信息
    cfg += "\nauth-user-pass /opt/vpngate-gateway/auth.txt\n"
    cfg += "script-security 2\n"
    cfg += "up /opt/vpngate-gateway/bin/vpn_up.sh\n"
    cfg += "down /opt/vpngate-gateway/bin/vpn_down.sh\n"

    fname = f"{node['country']}_{node['hostname'].split('.')[0]}.ovpn"
    path  = os.path.join(OVPN_DIR, fname)
    with open(path, "w") as f:
        f.write(cfg)
    node["ovpn_file"] = path
    return node

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", help="国家代码，逗号分隔，如 JP,US,KR")
    parser.add_argument("--top",     type=int, default=20, help="保留前N个节点")
    parser.add_argument("--no-ping", action="store_true", help="跳过延迟测试")
    args = parser.parse_args()

    country_filter = None
    if args.country:
        country_filter = [c.strip().upper() for c in args.country.split(",")]

    print(f"[*] 抓取节点列表...")
    raw   = fetch_raw()
    nodes = parse_nodes(raw, country_filter)
    print(f"[*] 获取到 {len(nodes)} 个节点" + (f"（已过滤: {args.country}）" if args.country else ""))

    if not args.no_ping:
        print(f"[*] 并发测试延迟...")
        nodes = test_nodes(nodes)
        # 过滤不可达节点，按延迟排序
        nodes = [n for n in nodes if n["ping"] < 5000]
        nodes.sort(key=lambda x: x["ping"])
    else:
        nodes.sort(key=lambda x: -x["speed"])

    nodes = nodes[:args.top]
    print(f"[*] 筛选后保留 {len(nodes)} 个节点")

    # 清理旧配置
    for f in os.listdir(OVPN_DIR):
        if f.endswith(".ovpn"):
            os.remove(os.path.join(OVPN_DIR, f))

    # 保存 ovpn 文件
    for n in nodes:
        save_ovpn(n)
        ping_str = f"{n['ping']}ms" if n['ping'] < 9999 else "N/A"
        print(f"    [{n['country']}] {n['hostname']} ({ping_str})")

    # 保存节点列表 JSON（去掉大 base64）
    for n in nodes:
        del n["b64"]
    with open(NODE_FILE, "w") as f:
        json.dump(nodes, f, indent=2)

    print(f"[✓] 节点已保存到 {OVPN_DIR}/")

if __name__ == "__main__":
    main()
PYEOF

# ── VPN 连接管理脚本 ──────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/vpn_manager.sh" << 'BASHEOF'
#!/bin/bash
# VPN 连接管理：启动/切换/监控 OpenVPN 节点

INSTALL_DIR="/opt/vpngate-gateway"
OVPN_DIR="$INSTALL_DIR/ovpn"
LOG_DIR="$INSTALL_DIR/logs"
PROXY_PORT=7928
ROUTE_TABLE=100
FWMARK=0x1
TUN_DEV="tun0"
CURRENT_FILE="$INSTALL_DIR/current_node"

# 写认证文件
echo "vpn" > "$INSTALL_DIR/auth.txt"
echo "vpn" >> "$INSTALL_DIR/auth.txt"
chmod 600 "$INSTALL_DIR/auth.txt"

stop_vpn() {
    pkill -f "openvpn.*vpngate" 2>/dev/null || true
    pkill microsocks 2>/dev/null || true
    # 清理路由
    ip rule del fwmark $FWMARK table $ROUTE_TABLE 2>/dev/null || true
    ip route flush table $ROUTE_TABLE 2>/dev/null || true
    sleep 1
}

setup_routing() {
    # 策略路由：打了 fwmark 的包走表100 → tun0
    ip rule add fwmark $FWMARK table $ROUTE_TABLE 2>/dev/null || true
    # 等 tun0 起来
    for i in $(seq 1 15); do
        ip link show $TUN_DEV &>/dev/null && break
        sleep 1
    done
    ip route add default dev $TUN_DEV table $ROUTE_TABLE 2>/dev/null || true
}

start_proxy() {
    # microsocks 绑定 tun0（SO_BINDTODEVICE），强制走 VPN 出口
    microsocks -i 127.0.0.1 -p $PROXY_PORT &
    echo $! > "$INSTALL_DIR/microsocks.pid"
    
    # iptables 给 microsocks 流量打 fwmark
    iptables -t mangle -A OUTPUT -p tcp --sport $PROXY_PORT -j MARK --set-mark $FWMARK 2>/dev/null || true
    iptables -t mangle -A OUTPUT -p tcp --dport $PROXY_PORT -j MARK --set-mark $FWMARK 2>/dev/null || true
}

connect() {
    local ovpn_file="$1"
    [[ -z "$ovpn_file" ]] && { echo "用法: connect <ovpn文件>"; return 1; }
    [[ ! -f "$ovpn_file" ]] && { echo "文件不存在: $ovpn_file"; return 1; }

    echo "[*] 停止旧连接..."
    stop_vpn

    echo "[*] 连接节点: $(basename $ovpn_file)"
    openvpn --config "$ovpn_file" \
            --daemon vpngate \
            --log "$LOG_DIR/openvpn.log" \
            --route-noexec \
            --redirect-gateway def1 bypass-dhcp \
            --script-security 2

    echo "[*] 等待 tun0 建立..."
    setup_routing

    echo "[*] 启动 SOCKS5 代理 (127.0.0.1:$PROXY_PORT)..."
    start_proxy

    echo "$ovpn_file" > "$CURRENT_FILE"
    echo "[✓] 连接成功！代理地址: 127.0.0.1:$PROXY_PORT"
}

# 自动选最快节点
auto_connect() {
    local country="${1:-}"
    ovpn_files=($(ls "$OVPN_DIR"/*.ovpn 2>/dev/null))
    [[ ${#ovpn_files[@]} -eq 0 ]] && { echo "没有可用节点，请先运行 fetch"; return 1; }
    
    if [[ -n "$country" ]]; then
        ovpn_files=($(ls "$OVPN_DIR"/${country^^}_*.ovpn 2>/dev/null))
        [[ ${#ovpn_files[@]} -eq 0 ]] && { echo "没有 $country 的节点"; return 1; }
    fi
    
    # nodes.json 已按延迟排序，取第一个匹配的
    connect "${ovpn_files[0]}"
}

# 轮换到下一个节点
rotate() {
    ovpn_files=($(ls "$OVPN_DIR"/*.ovpn 2>/dev/null))
    [[ ${#ovpn_files[@]} -eq 0 ]] && { echo "没有可用节点"; return 1; }
    
    current=$(cat "$CURRENT_FILE" 2>/dev/null || echo "")
    next=""
    found=false
    for f in "${ovpn_files[@]}"; do
        if $found; then next="$f"; break; fi
        [[ "$f" == "$current" ]] && found=true
    done
    [[ -z "$next" ]] && next="${ovpn_files[0]}"
    
    echo "[*] 轮换到: $(basename $next)"
    connect "$next"
}

case "${1:-}" in
    connect)  connect "$2" ;;
    auto)     auto_connect "$2" ;;
    rotate)   rotate ;;
    stop)     stop_vpn; echo "[✓] 已停止" ;;
    *)        echo "用法: $0 {connect <file>|auto [country]|rotate|stop}" ;;
esac
BASHEOF

# ── 主管理命令 ────────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/gateway.sh" << 'BASHEOF'
#!/bin/bash
# vpngw 主管理命令

INSTALL_DIR="/opt/vpngate-gateway"
NODE_FILE="$INSTALL_DIR/nodes.json"
PROXY_PORT=7928

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

show_status() {
    echo -e "${BOLD}═══════════════════════════════════${NC}"
    echo -e "${BOLD}  VPNGate Gateway 状态${NC}"
    echo -e "${BOLD}═══════════════════════════════════${NC}"

    # 当前节点
    current=$(cat "$INSTALL_DIR/current_node" 2>/dev/null || echo "未连接")
    echo -e "当前节点: ${CYAN}$(basename $current)${NC}"

    # tun0 状态
    if ip link show tun0 &>/dev/null; then
        tun_ip=$(ip addr show tun0 2>/dev/null | grep 'inet ' | awk '{print $2}')
        echo -e "VPN 隧道: ${GREEN}已连接${NC} ($tun_ip)"
    else
        echo -e "VPN 隧道: ${RED}未连接${NC}"
    fi

    # 代理状态
    if ss -tlnp | grep -q ":$PROXY_PORT "; then
        echo -e "代理地址: ${GREEN}127.0.0.1:$PROXY_PORT${NC} (SOCKS5)"
    else
        echo -e "代理地址: ${RED}未运行${NC}"
    fi

    # 出口 IP
    echo -n "出口 IP:  "
    out_ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo "获取失败")
    echo -e "${YELLOW}$out_ip${NC}"

    echo -e "${BOLD}═══════════════════════════════════${NC}"
}

show_nodes() {
    [[ ! -f "$NODE_FILE" ]] && { echo "节点列表为空，请先运行: vpngw fetch"; return; }
    echo -e "${BOLD}可用节点列表:${NC}"
    python3 -c "
import json
with open('$NODE_FILE') as f:
    nodes = json.load(f)
for i, n in enumerate(nodes, 1):
    ping = f\"{n['ping']}ms\" if n['ping'] < 9999 else 'N/A'
    speed = f\"{n['speed']//1000000}Mbps\"
    print(f\"  {i:2}. [{n['country']}] {n['hostname']} | {ping} | {speed}\")
"
}

menu() {
    while true; do
        echo ""
        echo -e "${BOLD}VPNGate Gateway 管理${NC}"
        echo "  1. 查看状态"
        echo "  2. 抓取节点（全部）"
        echo "  3. 抓取节点（指定国家）"
        echo "  4. 查看节点列表"
        echo "  5. 自动连接最快节点"
        echo "  6. 指定国家连接"
        echo "  7. 手动选择节点"
        echo "  8. 轮换下一个节点"
        echo "  9. 停止服务"
        echo "  0. 退出"
        echo -n "请选择: "
        read choice
        case $choice in
            1) show_status ;;
            2) python3 "$INSTALL_DIR/bin/fetch_nodes.py" ;;
            3) echo -n "输入国家代码（如 JP,US,KR）: "
               read cc
               python3 "$INSTALL_DIR/bin/fetch_nodes.py" --country "$cc" ;;
            4) show_nodes ;;
            5) bash "$INSTALL_DIR/bin/vpn_manager.sh" auto ;;
            6) echo -n "输入国家代码（如 JP）: "
               read cc
               bash "$INSTALL_DIR/bin/vpn_manager.sh" auto "$cc" ;;
            7) show_nodes
               echo -n "输入节点编号: "
               read num
               file=$(python3 -c "
import json
with open('$NODE_FILE') as f:
    nodes = json.load(f)
n = nodes[int('$num')-1]
print(n['ovpn_file'])
" 2>/dev/null)
               [[ -n "$file" ]] && bash "$INSTALL_DIR/bin/vpn_manager.sh" connect "$file" ;;
            8) bash "$INSTALL_DIR/bin/vpn_manager.sh" rotate ;;
            9) bash "$INSTALL_DIR/bin/vpn_manager.sh" stop ;;
            0) exit 0 ;;
            *) echo "无效选项" ;;
        esac
    done
}

case "${1:-menu}" in
    status)  show_status ;;
    fetch)   shift; python3 "$INSTALL_DIR/bin/fetch_nodes.py" "$@" ;;
    nodes)   show_nodes ;;
    auto)    bash "$INSTALL_DIR/bin/vpn_manager.sh" auto "${2:-}" ;;
    connect) bash "$INSTALL_DIR/bin/vpn_manager.sh" connect "$2" ;;
    rotate)  bash "$INSTALL_DIR/bin/vpn_manager.sh" rotate ;;
    stop)    bash "$INSTALL_DIR/bin/vpn_manager.sh" stop ;;
    menu)    menu ;;
    *)       echo "用法: vpngw {status|fetch|nodes|auto|connect|rotate|stop|menu}" ;;
esac
BASHEOF

# ── systemd 服务（开机自启 + 自动轮换）──────────────────────────────────────
cat > /etc/systemd/system/$SERVICE_NAME.service << SVCEOF
[Unit]
Description=VPNGate Gateway Service
After=network.target

[Service]
Type=forking
ExecStartPre=/usr/bin/python3 $INSTALL_DIR/bin/fetch_nodes.py --top 10
ExecStart=/bin/bash $INSTALL_DIR/bin/vpn_manager.sh auto
ExecStop=/bin/bash $INSTALL_DIR/bin/vpn_manager.sh stop
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

# ── 自动轮换定时器（每2小时换一次节点）──────────────────────────────────────
cat > /etc/systemd/system/vpngate-rotate.service << SVCEOF
[Unit]
Description=VPNGate Node Rotation

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_DIR/bin/vpn_manager.sh rotate
SVCEOF

cat > /etc/systemd/system/vpngate-rotate.timer << SVCEOF
[Unit]
Description=Rotate VPNGate node every 2 hours

[Timer]
OnBootSec=2h
OnUnitActiveSec=2h

[Install]
WantedBy=timers.target
SVCEOF

# ── 权限 ──────────────────────────────────────────────────────────────────────
chmod +x "$INSTALL_DIR/bin/"*.sh "$INSTALL_DIR/bin/"*.py

# ── 全局命令 ──────────────────────────────────────────────────────────────────
ln -sf "$INSTALL_DIR/bin/gateway.sh" /usr/local/bin/vpngw

# ── 启用服务 ──────────────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable $SERVICE_NAME vpngate-rotate.timer

step "安装完成"
echo -e "${GREEN}"
echo "  vpngw          - 打开交互菜单"
echo "  vpngw status   - 查看状态"
echo "  vpngw fetch    - 抓取节点"
echo "  vpngw auto     - 自动连接最快节点"
echo "  vpngw auto JP  - 连接日本节点"
echo "  vpngw rotate   - 切换下一个节点"
echo "  vpngw stop     - 停止服务"
echo ""
echo "  SOCKS5 代理: 127.0.0.1:7928  (给 Xray/3x-ui 配置出站)"
echo -e "${NC}"

echo -n "是否立即启动并连接节点？[Y/n] "
read ans
if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
    systemctl start $SERVICE_NAME
    sleep 3
    bash "$INSTALL_DIR/bin/gateway.sh" status
fi
