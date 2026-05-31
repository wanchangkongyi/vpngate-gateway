#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
C='\033[0;36m'; B='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

[[ $EUID -ne 0 ]] && echo -e "${R}请用 root 权限运行${NC}" && exit 1

echo -e "${B}"
cat << 'EOF'
 __   ____  _  _  ___  __  ____  ____     _  _  ___
 \ \ / /  \| \| |/ __||  \| ___||  _ \   | \| ||_ _|
  \ V /| () | .` | (_ || .` | _|  |   /   | .` | | |
   \_/  \__/|_|\_|\___||_|\_|___| |_|_\   |_|\_||___|
EOF
echo -e "${NC}"

INSTALL_DIR="/opt/vpngate-v2"
GITHUB_URL="https://github.com/你的用户名/vpngate-v2.git"

echo -e "${Y}[1/4] 安装依赖...${NC}"
apt-get update -qq
apt-get install -y -qq openvpn curl python3 iproute2 iptables net-tools git

echo -e "${Y}[2/4] 部署源码...${NC}"
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
    git fetch --all -q
    git reset --hard origin/main -q 2>/dev/null || git reset --hard origin/master -q
else
    git clone "$GITHUB_URL" "$INSTALL_DIR" -q
fi

echo -e "${Y}[3/4] 配置服务...${NC}"

# 数据目录
mkdir -p "$INSTALL_DIR/data"

# systemd 服务
cat > /lib/systemd/system/vpngate-v2.service << EOF2
[Unit]
Description=VPNGate Gateway v2
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/vpngate_manager.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF2

# 命令分发器（处理 vg 发出的命令）
cat > "$INSTALL_DIR/cmd_handler.py" << 'PYEOF'
#!/usr/bin/env python3
"""后台命令处理线程，读取 cmd.json 并执行"""
import json, os, sys, time, threading
sys.path.insert(0, "/opt/vpngate-v2")
import vpngate_manager as mgr

CMD_FILE    = mgr.DATA_DIR / "cmd.json"
RESULT_FILE = mgr.DATA_DIR / "cmd_result.json"
_last_ts    = 0.0

def handle(cmd: dict):
    global _last_ts
    ts = cmd.get("ts", 0)
    if ts <= _last_ts:
        return
    _last_ts = ts
    action = cmd.get("cmd", "")
    args   = cmd.get("args", [])
    ok, msg = False, "未知命令"
    try:
        if action == "auto_connect":
            ok, msg = mgr.auto_connect()
        elif action == "connect" and args:
            ok, msg = mgr.connect_node(args[0])
        elif action == "rotate":
            ok, msg = mgr.rotate_node()
        elif action == "disconnect":
            mgr.disconnect()
            ok, msg = True, "已断开"
    except Exception as e:
        msg = str(e)
    mgr._write_json(RESULT_FILE, {"cmd": action, "ok": ok, "msg": msg, "ts": time.time()})

def loop():
    while True:
        time.sleep(0.3)
        try:
            if CMD_FILE.exists():
                cmd = json.loads(CMD_FILE.read_text())
                handle(cmd)
        except Exception:
            pass

if __name__ == "__main__":
    loop()
PYEOF

# 把命令处理器集成到 manager 里
cat >> "$INSTALL_DIR/vpngate_manager.py" << 'PYEOF'

# 启动命令处理线程
import importlib.util as _iu, threading as _th
def _start_cmd_handler():
    import sys as _sys
    _sys.path.insert(0, "/opt/vpngate-v2")
    try:
        import cmd_handler
        _th.Thread(target=cmd_handler.loop, daemon=True).start()
    except Exception as e:
        log(f"[命令处理] 启动失败: {e}")

# 在 init() 里调用
_orig_init = init
def init():
    _orig_init()
    _start_cmd_handler()
PYEOF

# vg 命令
cp "$INSTALL_DIR/vg" /usr/local/bin/vg
chmod +x /usr/local/bin/vg
chmod +x "$INSTALL_DIR"/*.py

systemctl daemon-reload
systemctl enable vpngate-v2 --quiet

echo -e "${Y}[4/4] 初始化配置...${NC}"

# 询问初始设置
echo ""
echo -e "${C}── 初始化设置 ──────────────────────────────${NC}"
read -p "国家过滤（如 JP,US,KR，留空=不限）: " cc_input
read -p "自动轮换间隔（小时，0=关闭，默认2）: " rot_input
read -p "保留节点数量（默认20）: " top_input

CC_FILTER="[]"
if [ -n "$cc_input" ]; then
    CC_FILTER=$(python3 -c "
import json, sys
codes = [x.strip().upper() for x in '$cc_input'.split(',') if x.strip()]
print(json.dumps(codes))
")
fi
ROT_HOURS=${rot_input:-2}
TOP_NODES=${top_input:-20}

python3 -c "
import json
s = {
    'country_filter': $CC_FILTER,
    'rotate_hours':   int('$ROT_HOURS'),
    'top_nodes':      int('$TOP_NODES'),
    'proxy_port':     7928,
}
import os; os.makedirs('/opt/vpngate-v2/data', exist_ok=True)
with open('/opt/vpngate-v2/data/settings.json', 'w') as f:
    json.dump(s, f, indent=2)
print('设置已保存')
"

echo ""
echo -e "${G}╔══════════════════════════════════════════╗${NC}"
echo -e "${G}║          安装完成！                      ║${NC}"
echo -e "${G}╠══════════════════════════════════════════╣${NC}"
echo -e "${G}║${NC}  输入 ${BOLD}vg${NC} 打开交互管理菜单"
echo -e "${G}║${NC}  ${C}vg status${NC}   查看状态"
echo -e "${G}║${NC}  ${C}vg nodes${NC}    查看节点列表"
echo -e "${G}║${NC}  ${C}vg auto${NC}     自动连接最快节点"
echo -e "${G}║${NC}  ${C}vg rotate${NC}   轮换节点"
echo -e "${G}║${NC}  ${C}vg logs${NC}     查看日志"
echo -e "${G}║${NC}  SOCKS5代理: ${C}127.0.0.1:7928${NC}"
echo -e "${G}╚══════════════════════════════════════════╝${NC}"
echo ""

read -p "是否立即启动服务？[Y/n] " start_now
if [[ "${start_now:-Y}" =~ ^[Yy]$ ]]; then
    systemctl start vpngate-v2
    sleep 3
    echo -e "${G}服务已启动，输入 vg 查看状态${NC}"
fi
