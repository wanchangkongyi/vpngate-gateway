#!/bin/bash
# VPNGate Gateway 一键安装脚本

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; PLAIN='\033[0m'; BOLD='\033[1m'

info()  { echo -e "${GREEN}[INFO]${PLAIN} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${PLAIN} $*"; }
error() { echo -e "${RED}[ERROR]${PLAIN} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "请用 root 权限运行: sudo bash install.sh"

INSTALL_DIR="/opt/vpngate-gateway"
SERVICE_NAME="vpngate-gateway"
PROXY_PORT=7928

echo -e "${CYAN}"
cat << 'BANNER'
  _   _______  _   _  _____       _
 | | | | ___ \| \ | ||  __ \     | |
 | | | | |_/ /|  \| || |  \/ __ _| |_ _____      ____ _ _   _
 | | | |  __/ | . ` || | __ / _` | __/ _ \ \ /\ / / _` | | | |
 \ \_/ / |    | |\  || |_\ \ (_| | ||  __/\ V  V / (_| | |_| |
  \___/\_|    \_| \_/ \____/\__,_|\__\___| \_/\_/ \__,_|\__, |
                                                          __/ |
                                                         |___/
BANNER
echo -e "${PLAIN}"

echo -e "正在安装依赖..."
apt-get update -qq
apt-get install -y -qq openvpn curl python3 iproute2 iptables net-tools

# 安装 microsocks
if ! command -v microsocks &>/dev/null; then
    apt-get install -y -qq gcc make
    cd /tmp
    curl -sL https://github.com/rofl0r/microsocks/archive/refs/heads/master.tar.gz | tar xz
    cd microsocks-master && make -s && make install -s
    cd /
fi

mkdir -p "$INSTALL_DIR"/{bin,ovpn,logs}

# ── 写认证文件 ────────────────────────────────────────────────────────────────
echo -e "vpn\nvpn" > "$INSTALL_DIR/auth.txt"
chmod 600 "$INSTALL_DIR/auth.txt"

# ── 节点抓取脚本 ──────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/fetch_nodes.py" << 'PYEOF'
#!/usr/bin/env python3
import csv, io, base64, os, sys, json, re, socket, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request

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
        lines = cfg.splitlines()
        ports = []
        for line in lines:
            m = re.match(r'^remote\s+\S+\s+(\d+)', line.strip())
            if m:
                ports.append(int(m.group(1)))
        if not ports:
            return 443
        unique = list(dict.fromkeys(ports))
        if len(unique) == 1:
            return unique[0]
        udp = {1194,1195,1196,1197,1198}
        tcp = [p for p in unique if p not in udp]
        return tcp[0] if tcp else ports[0]
    except:
        return 443

def ping_host(ip, port=443, timeout=3):
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
            "hostname": hostname, "ip": ip, "port": port,
            "country": cc, "speed": speed, "b64": b64, "ping": 9999,
        })
    return nodes

def test_nodes(nodes, workers=30):
    def _test(node):
        node["ping"] = ping_host(node["ip"], node["port"])
        return node
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_test, n): n for n in nodes}
        results = [f.result() for f in as_completed(futures)]
    return results

def save_ovpn(node):
    cfg = base64.b64decode(node["b64"]).decode("utf-8", errors="replace")
    cfg = re.sub(
        r'^(remote\s+)\S+(\s+\d+)',
        lambda m: m.group(1) + node["hostname"] + m.group(2).rstrip(),
        cfg, flags=re.MULTILINE
    )
    cfg += "\nauth-user-pass /opt/vpngate-gateway/auth.txt\n"
    cfg += "script-security 2\n"
    fname = f"{node['country']}_{node['hostname'].split('.')[0]}.ovpn"
    path  = os.path.join(OVPN_DIR, fname)
    with open(path, "w") as f:
        f.write(cfg)
    node["ovpn_file"] = path
    return node

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", help="国家代码，逗号分隔")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--no-ping", action="store_true")
    args = parser.parse_args()

    country_filter = [c.strip().upper() for c in args.country.split(",")] if args.country else None

    print(f"  正在抓取节点列表...")
    raw   = fetch_raw()
    nodes = parse_nodes(raw, country_filter)
    print(f"  获取到 {len(nodes)} 个节点")

    if not args.no_ping:
        print(f"  并发测试延迟（{min(30,len(nodes))} 线程）...")
        nodes = test_nodes(nodes)
        nodes = [n for n in nodes if n["ping"] < 5000]
        nodes.sort(key=lambda x: x["ping"])
    else:
        nodes.sort(key=lambda x: -x["speed"])

    nodes = nodes[:args.top]

    for f in os.listdir(OVPN_DIR):
        if f.endswith(".ovpn"):
            os.remove(os.path.join(OVPN_DIR, f))

    for n in nodes:
        save_ovpn(n)

    saved = []
    for n in nodes:
        ping_str = f"{n['ping']}ms" if n['ping'] < 9999 else "N/A"
        speed_str = f"{n['speed']//1000000}Mbps"
        print(f"  [{n['country']}] {n['hostname']}  {ping_str}  {speed_str}")
        del n["b64"]
        saved.append(n)

    with open(NODE_FILE, "w") as f:
        json.dump(saved, f, indent=2)
    print(f"\n  已保存 {len(saved)} 个节点")

if __name__ == "__main__":
    main()
PYEOF

# ── VPN 连接管理 ──────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/vpn_manager.sh" << 'BASHEOF'
#!/bin/bash
INSTALL_DIR="/opt/vpngate-gateway"
PROXY_PORT=7928
ROUTE_TABLE=100
FWMARK=0x1
TUN_DEV="tun0"
CURRENT_FILE="$INSTALL_DIR/current_node"

stop_vpn() {
    pkill -f "openvpn.*vpngate" 2>/dev/null || true
    pkill microsocks 2>/dev/null || true
    ip rule del fwmark $FWMARK table $ROUTE_TABLE 2>/dev/null || true
    ip route flush table $ROUTE_TABLE 2>/dev/null || true
    iptables -t mangle -F OUTPUT 2>/dev/null || true
    sleep 1
}

setup_routing() {
    ip rule add fwmark $FWMARK table $ROUTE_TABLE 2>/dev/null || true
    for i in $(seq 1 20); do
        ip link show $TUN_DEV &>/dev/null && break
        sleep 1
    done
    ip route add default dev $TUN_DEV table $ROUTE_TABLE 2>/dev/null || true
}

start_proxy() {
    microsocks -i 127.0.0.1 -p $PROXY_PORT &
    echo $! > "$INSTALL_DIR/microsocks.pid"
    iptables -t mangle -A OUTPUT -p tcp --sport $PROXY_PORT -j MARK --set-mark $FWMARK 2>/dev/null || true
    iptables -t mangle -A OUTPUT -p tcp --dport $PROXY_PORT -j MARK --set-mark $FWMARK 2>/dev/null || true
}

connect() {
    local ovpn_file="$1"
    [[ -z "$ovpn_file" || ! -f "$ovpn_file" ]] && { echo "文件不存在: $ovpn_file"; return 1; }
    stop_vpn
    openvpn --config "$ovpn_file" \
            --daemon vpngate \
            --log "$INSTALL_DIR/logs/openvpn.log" \
            --route-noexec \
            --script-security 2
    setup_routing
    start_proxy
    echo "$ovpn_file" > "$CURRENT_FILE"
}

auto_connect() {
    local country="${1:-}"
    if [[ -n "$country" ]]; then
        ovpn_files=($(ls "$INSTALL_DIR/ovpn"/${country^^}_*.ovpn 2>/dev/null))
    else
        ovpn_files=($(ls "$INSTALL_DIR/ovpn"/*.ovpn 2>/dev/null))
    fi
    [[ ${#ovpn_files[@]} -eq 0 ]] && { echo "没有可用节点"; return 1; }
    connect "${ovpn_files[0]}"
}

rotate() {
    ovpn_files=($(ls "$INSTALL_DIR/ovpn"/*.ovpn 2>/dev/null))
    [[ ${#ovpn_files[@]} -eq 0 ]] && { echo "没有可用节点"; return 1; }
    current=$(cat "$CURRENT_FILE" 2>/dev/null || echo "")
    next=""
    found=false
    for f in "${ovpn_files[@]}"; do
        if $found; then next="$f"; break; fi
        [[ "$f" == "$current" ]] && found=true
    done
    [[ -z "$next" ]] && next="${ovpn_files[0]}"
    connect "$next"
}

case "${1:-}" in
    connect)  connect "$2" ;;
    auto)     auto_connect "${2:-}" ;;
    rotate)   rotate ;;
    stop)     stop_vpn ;;
esac
BASHEOF

# ── 主菜单命令（3x-ui 风格）──────────────────────────────────────────────────
cat > "$INSTALL_DIR/bin/gateway.sh" << 'BASHEOF'
#!/bin/bash

INSTALL_DIR="/opt/vpngate-gateway"
NODE_FILE="$INSTALL_DIR/nodes.json"
PROXY_PORT=7928

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PLAIN='\033[0m'
BOLD='\033[1m'

# 获取当前状态信息
get_status() {
    # tun0
    if ip link show tun0 &>/dev/null 2>&1; then
        VPN_STATUS="${GREEN}已连接${PLAIN}"
    else
        VPN_STATUS="${RED}未连接${PLAIN}"
    fi

    # 当前节点
    if [[ -f "$INSTALL_DIR/current_node" ]]; then
        cur=$(cat "$INSTALL_DIR/current_node")
        CURRENT_NODE=$(basename "$cur" .ovpn)
    else
        CURRENT_NODE="无"
    fi

    # 代理
    if ss -tlnp 2>/dev/null | grep -q ":$PROXY_PORT "; then
        PROXY_STATUS="${GREEN}运行中${PLAIN}  127.0.0.1:$PROXY_PORT"
    else
        PROXY_STATUS="${RED}未运行${PLAIN}"
    fi

    # 出口 IP
    OUT_IP=$(curl -s --max-time 4 https://api.ipify.org 2>/dev/null || echo "获取失败")
}

# 显示主菜单
show_menu() {
    get_status
    clear
    echo -e "${BOLD}${BLUE}╔════════════════════════════════════════════╗${PLAIN}"
    echo -e "${BOLD}${BLUE}║        VPNGate Gateway 管理面板            ║${PLAIN}"
    echo -e "${BOLD}${BLUE}╠════════════════════════════════════════════╣${PLAIN}"
    echo -e "${BOLD}${BLUE}║${PLAIN}  VPN 状态  : $VPN_STATUS"
    echo -e "${BOLD}${BLUE}║${PLAIN}  当前节点  : ${CYAN}$CURRENT_NODE${PLAIN}"
    echo -e "${BOLD}${BLUE}║${PLAIN}  代理状态  : $PROXY_STATUS"
    echo -e "${BOLD}${BLUE}║${PLAIN}  出口 IP   : ${YELLOW}$OUT_IP${PLAIN}"
    echo -e "${BOLD}${BLUE}╠════════════════════════════════════════════╣${PLAIN}"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}0.${PLAIN}  退出"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}1.${PLAIN}  抓取节点（全部）"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}2.${PLAIN}  抓取节点（指定国家）"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}3.${PLAIN}  查看节点列表"
    echo -e "${BOLD}${BLUE}╠════════════════════════════════════════════╣${PLAIN}"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}4.${PLAIN}  自动连接最快节点"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}5.${PLAIN}  指定国家连接"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}6.${PLAIN}  手动选择节点"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}7.${PLAIN}  轮换下一个节点"
    echo -e "${BOLD}${BLUE}╠════════════════════════════════════════════╣${PLAIN}"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${GREEN}8.${PLAIN}  查看 OpenVPN 日志"
    echo -e "${BOLD}${BLUE}║${PLAIN}  ${RED}9.${PLAIN}  停止服务"
    echo -e "${BOLD}${BLUE}╚════════════════════════════════════════════╝${PLAIN}"
    echo -en "  请输入选项 [0-9]: "
}

# 显示节点列表
show_nodes() {
    [[ ! -f "$NODE_FILE" ]] && echo -e "  ${YELLOW}节点列表为空，请先抓取节点（选项1）${PLAIN}" && return
    echo ""
    echo -e "${BOLD}${BLUE}╔════════════════════════════════════════════════════════════╗${PLAIN}"
    echo -e "${BOLD}${BLUE}║  序号   国家   主机名                          延迟    速度  ║${PLAIN}"
    echo -e "${BOLD}${BLUE}╠════════════════════════════════════════════════════════════╣${PLAIN}"
    python3 -c "
import json
with open('$NODE_FILE') as f:
    nodes = json.load(f)
for i, n in enumerate(nodes, 1):
    ping  = f\"{n['ping']}ms\" if n['ping'] < 9999 else 'N/A'
    speed = f\"{n['speed']//1000000}Mbps\"
    name  = n['hostname'].split('.')[0][:35]
    print(f\"\033[0;34m║\033[0m  {i:3}.  [{n['country']:2}]  {name:<36} {ping:>6}  {speed:>6}  \033[0;34m║\033[0m\")
"
    echo -e "${BOLD}${BLUE}╚════════════════════════════════════════════════════════════╝${PLAIN}"
}

# 手动选择节点
select_node() {
    show_nodes
    [[ ! -f "$NODE_FILE" ]] && return
    echo ""
    echo -en "  请输入节点编号: "
    read num
    [[ -z "$num" || ! "$num" =~ ^[0-9]+$ ]] && echo "  无效输入" && return
    file=$(python3 -c "
import json
with open('$NODE_FILE') as f:
    nodes = json.load(f)
idx = int('$num') - 1
if 0 <= idx < len(nodes):
    print(nodes[idx].get('ovpn_file',''))
" 2>/dev/null)
    if [[ -n "$file" && -f "$file" ]]; then
        echo -e "  ${CYAN}正在连接: $(basename $file)${PLAIN}"
        bash "$INSTALL_DIR/bin/vpn_manager.sh" connect "$file"
        echo -e "  ${GREEN}连接成功！${PLAIN}"
    else
        echo -e "  ${RED}节点文件不存在，请重新抓取节点${PLAIN}"
    fi
}

# 主循环
main() {
    while true; do
        show_menu
        read choice
        echo ""
        case "$choice" in
            0)
                echo -e "  ${GREEN}再见！${PLAIN}"
                exit 0
                ;;
            1)
                echo -e "  ${CYAN}正在抓取全部节点...${PLAIN}"
                python3 "$INSTALL_DIR/bin/fetch_nodes.py"
                ;;
            2)
                echo -en "  请输入国家代码（如 JP,US,KR）: "
                read cc
                echo -e "  ${CYAN}正在抓取 $cc 节点...${PLAIN}"
                python3 "$INSTALL_DIR/bin/fetch_nodes.py" --country "$cc"
                ;;
            3)
                show_nodes
                ;;
            4)
                echo -e "  ${CYAN}正在连接最快节点...${PLAIN}"
                bash "$INSTALL_DIR/bin/vpn_manager.sh" auto
                echo -e "  ${GREEN}连接成功！代理: 127.0.0.1:$PROXY_PORT${PLAIN}"
                ;;
            5)
                echo -en "  请输入国家代码（如 JP）: "
                read cc
                echo -e "  ${CYAN}正在连接 $cc 节点...${PLAIN}"
                bash "$INSTALL_DIR/bin/vpn_manager.sh" auto "$cc"
                echo -e "  ${GREEN}连接成功！${PLAIN}"
                ;;
            6)
                select_node
                ;;
            7)
                echo -e "  ${CYAN}正在轮换节点...${PLAIN}"
                bash "$INSTALL_DIR/bin/vpn_manager.sh" rotate
                echo -e "  ${GREEN}已切换到下一个节点${PLAIN}"
                ;;
            8)
                echo -e "  ${CYAN}OpenVPN 日志（按 q 退出）:${PLAIN}"
                tail -50 "$INSTALL_DIR/logs/openvpn.log" 2>/dev/null | less
                ;;
            9)
                echo -e "  ${YELLOW}正在停止服务...${PLAIN}"
                bash "$INSTALL_DIR/bin/vpn_manager.sh" stop
                echo -e "  ${GREEN}已停止${PLAIN}"
                ;;
            *)
                echo -e "  ${RED}无效选项，请输入 0-9${PLAIN}"
                ;;
        esac
        echo ""
        echo -en "  按 Enter 返回菜单..."
        read
    done
}

# 支持直接带参数执行，不进菜单
case "${1:-}" in
    status)
        get_status
        echo -e "VPN: $VPN_STATUS | 节点: $CURRENT_NODE | 代理: $PROXY_STATUS | 出口IP: $OUT_IP"
        ;;
    fetch)  shift; python3 "$INSTALL_DIR/bin/fetch_nodes.py" "$@" ;;
    auto)   bash "$INSTALL_DIR/bin/vpn_manager.sh" auto "${2:-}" ;;
    rotate) bash "$INSTALL_DIR/bin/vpn_manager.sh" rotate ;;
    stop)   bash "$INSTALL_DIR/bin/vpn_manager.sh" stop ;;
    "")     main ;;
    *)      echo "用法: vpngw {status|fetch|auto [国家]|rotate|stop} 或直接输入 vpngw 进入菜单" ;;
esac
BASHEOF

chmod +x "$INSTALL_DIR/bin/"*.sh "$INSTALL_DIR/bin/"*.py

# ── systemd 服务 ──────────────────────────────────────────────────────────────
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

ln -sf "$INSTALL_DIR/bin/gateway.sh" /usr/local/bin/vpngw

systemctl daemon-reload
systemctl enable $SERVICE_NAME vpngate-rotate.timer

echo -e "\n${GREEN}╔════════════════════════════════════╗${PLAIN}"
echo -e "${GREEN}║        安装完成！                  ║${PLAIN}"
echo -e "${GREEN}╠════════════════════════════════════╣${PLAIN}"
echo -e "${GREEN}║${PLAIN}  输入 ${BOLD}vpngw${PLAIN} 打开管理菜单          "
echo -e "${GREEN}╚════════════════════════════════════╝${PLAIN}\n"

echo -en "是否立即启动并连接节点？[Y/n] "
read ans
if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
    bash "$INSTALL_DIR/bin/gateway.sh" fetch
    bash "$INSTALL_DIR/bin/vpn_manager.sh" auto
    bash "$INSTALL_DIR/bin/gateway.sh" status
fi
