# VPNGate Gateway

将 [VPNGate](https://www.vpngate.net) 免费公共节点作为 VPS 出口 IP，本地暴露 SOCKS5/HTTP 双协议代理供 Xray/3x-ui 使用。出口 IP 持续轮换，SSH 管理流量走 eth0 物理网卡，**不会断连**。

## 架构

```
Xray / 3x-ui
    │ SOCKS5 出站
    ▼
127.0.0.1:7928  ← HTTP/SOCKS5 双协议代理（proxy_server.py）
    │ SO_BINDTODEVICE → tun0
    ▼
tun0  ← OpenVPN 虚拟网卡
    │ 策略路由表 100（仅 tun0 出站流量走此表）
    ▼
VPNGate 公共节点（日本 / 美国 / 韩国 / ...）
    │
    ▼
目标网站（看到 VPNGate 节点 IP，非 VPS 真实 IP）
```

SSH / 管理流量始终走 eth0，与 tun0 路由表完全隔离。

## 快速安装

```bash
# 修改 install.sh 顶部的 GITHUB_REPO 为你的仓库后执行
bash <(curl -Ls https://raw.githubusercontent.com/wanchangkongyi/vpngate-gateway/main/install.sh)
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `vpngw` | 打开交互菜单 |
| `vpngw status` | 查看状态 |
| `vpngw status --check` | 查看状态并实时检测出口 IP |
| `vpngw fetch` | 抓取全部节点并测速/验证 |
| `vpngw fetch --country JP,US` | 只抓取日本/美国节点 |
| `vpngw fetch --top 30` | 保留前 30 个节点 |
| `vpngw auto` | 自动连接最快节点 |
| `vpngw auto JP` | 自动连接最快日本节点 |
| `vpngw nodes` | 查看节点列表 |
| `vpngw nodes --country JP` | 只看日本节点 |
| `vpngw connect <node_id>` | 连接指定节点 |
| `vpngw rotate` | 切换到下一个节点 |
| `vpngw stop` | 断开 VPN 连接 |
| `vpngw logs` | 查看最近 50 行日志 |
| `vpngw logs -n 100` | 查看最近 100 行日志 |
| `vpngw set <key> <value>` | 修改设置 |

## 设置项

| 设置项 | 默认值 | 说明 |
|--------|--------|------|
| `country_filter` | `[]` | 国家白名单，空=不限（如 `JP,US,KR`） |
| `rotate_hours` | `2` | 自动轮换间隔（小时），`0`=关闭 |
| `top_nodes` | `20` | 保留前 N 个延迟最低的节点 |
| `probe_count` | `10` | OpenVPN 握手验证节点数 |
| `proxy_port` | `7928` | 代理监听端口 |
| `auto_fetch_on_start` | `true` | 服务启动时自动抓取节点 |

修改示例：
```bash
vpngw set rotate_hours 1        # 每1小时轮换
vpngw set country_filter JP,KR  # 只用日本/韩国节点
vpngw set top_nodes 30          # 保留前30个节点
vpngw set rotate_hours 0        # 关闭自动轮换
```

## Xray / 3x-ui 出站配置

在 3x-ui 面板添加出站代理（SOCKS5）：

```json
{
  "tag": "vpngate-out",
  "protocol": "socks",
  "settings": {
    "servers": [{
      "address": "127.0.0.1",
      "port": 7928
    }]
  }
}
```

也可以使用 HTTP 代理模式（同一端口）：

```json
{
  "tag": "vpngate-out",
  "protocol": "http",
  "settings": {
    "servers": [{
      "address": "127.0.0.1",
      "port": 7928
    }]
  }
}
```

## 服务管理

```bash
systemctl status vpngate-gateway    # 查看服务状态
systemctl restart vpngate-gateway   # 重启服务
systemctl stop vpngate-gateway      # 停止服务
journalctl -u vpngate-gateway -f    # 实时查看 systemd 日志
```

## 文件结构

```
/opt/vpngate-gateway/
├── vpngate_manager.py   # 核心管理器（主进程）
├── proxy_server.py      # HTTP/SOCKS5 代理
├── vpn_utils.py         # 工具函数（抓取/测速/解析）
├── vpngw                # CLI 命令
└── data/
    ├── nodes.json       # 节点列表（含延迟/验证状态）
    ├── state.json       # 运行状态
    ├── settings.json    # 用户设置
    ├── vpngate.log      # 日志（超 5MB 自动轮转）
    ├── cmd.json         # CLI → 后台命令通道
    ├── cmd_result.json  # 后台 → CLI 结果通道
    └── configs/         # OpenVPN 配置文件缓存
```

## 设计说明

### 流量隔离
代理服务器通过 `SO_BINDTODEVICE` 将所有出站连接绑定到 `tun0`。若 `tun0` 不存在（VPN 断开），代理直接返回 502，**绝不回落到真实 IP**，避免 IP 泄露。

### Watchdog 重连退避
OpenVPN 进程意外退出后，Watchdog 采用指数退避策略重连（30s → 60s → 120s → ... → 最大 10 分钟），避免因节点不可用导致的连接风暴。

### 命令总线
`vpngw` CLI 通过文件（`cmd.json` / `cmd_result.json`）与后台进程通信，无需 socket 或 IPC，兼容性强且简单可靠。

### 节点筛选逻辑
1. TCP 延迟测试（并发 50 线程）
2. OpenVPN 握手验证（并发 5 线程，避免资源耗尽）
3. 优先连接 `probe_status=available` 的节点
4. 相同优先级按延迟升序排列

## 常见问题

**Q: 安装后 `vpngw status` 显示未连接？**  
A: 首次启动需要几分钟抓取和测试节点。运行 `vpngw logs` 查看进度。

**Q: OpenVPN AUTH_FAILED 是什么意思？**  
A: 部分 VPNGate 节点要求特定凭证，系统会自动跳过这类节点选下一个。

**Q: 如何只用延迟 < 100ms 的节点？**  
A: 当前版本按延迟排序自动选择最低延迟节点，`top_nodes` 设置决定候选池大小。

**Q: 可以同时给多个 Xray 出站使用吗？**  
A: 代理服务器支持高并发（256 连接队列），完全可以。

## 依赖

- Linux（Ubuntu 20.04+/Debian 10+）
- Python 3.8+
- OpenVPN 2.4+
- iproute2、curl、git/vpngate-gateway/main/install.sh)
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `vpngw` | 打开交互菜单 |
| `vpngw status` | 查看状态 |
| `vpngw status --check` | 查看状态并实时检测出口 IP |
| `vpngw fetch` | 抓取全部节点并测速/验证 |
| `vpngw fetch --country JP,US` | 只抓取日本/美国节点 |
| `vpngw fetch --top 30` | 保留前 30 个节点 |
| `vpngw auto` | 自动连接最快节点 |
| `vpngw auto JP` | 自动连接最快日本节点 |
| `vpngw nodes` | 查看节点列表 |
| `vpngw nodes --country JP` | 只看日本节点 |
| `vpngw connect <node_id>` | 连接指定节点 |
| `vpngw rotate` | 切换到下一个节点 |
| `vpngw stop` | 断开 VPN 连接 |
| `vpngw logs` | 查看最近 50 行日志 |
| `vpngw logs -n 100` | 查看最近 100 行日志 |
| `vpngw set <key> <value>` | 修改设置 |

## 设置项

| 设置项 | 默认值 | 说明 |
|--------|--------|------|
| `country_filter` | `[]` | 国家白名单，空=不限（如 `JP,US,KR`） |
| `rotate_hours` | `2` | 自动轮换间隔（小时），`0`=关闭 |
| `top_nodes` | `20` | 保留前 N 个延迟最低的节点 |
| `probe_count` | `10` | OpenVPN 握手验证节点数 |
| `proxy_port` | `7928` | 代理监听端口 |
| `auto_fetch_on_start` | `true` | 服务启动时自动抓取节点 |

修改示例：
```bash
vpngw set rotate_hours 1        # 每1小时轮换
vpngw set country_filter JP,KR  # 只用日本/韩国节点
vpngw set top_nodes 30          # 保留前30个节点
vpngw set rotate_hours 0        # 关闭自动轮换
```

## Xray / 3x-ui 出站配置

在 3x-ui 面板添加出站代理（SOCKS5）：

```json
{
  "tag": "vpngate-out",
  "protocol": "socks",
  "settings": {
    "servers": [{
      "address": "127.0.0.1",
      "port": 7928
    }]
  }
}
```

也可以使用 HTTP 代理模式（同一端口）：

```json
{
  "tag": "vpngate-out",
  "protocol": "http",
  "settings": {
    "servers": [{
      "address": "127.0.0.1",
      "port": 7928
    }]
  }
}
```

## 服务管理

```bash
systemctl status vpngate-gateway    # 查看服务状态
systemctl restart vpngate-gateway   # 重启服务
systemctl stop vpngate-gateway      # 停止服务
journalctl -u vpngate-gateway -f    # 实时查看 systemd 日志
```

## 文件结构

```
/opt/vpngate-gateway/
├── vpngate_manager.py   # 核心管理器（主进程）
├── proxy_server.py      # HTTP/SOCKS5 代理
├── vpn_utils.py         # 工具函数（抓取/测速/解析）
├── vpngw                # CLI 命令
└── data/
    ├── nodes.json       # 节点列表（含延迟/验证状态）
    ├── state.json       # 运行状态
    ├── settings.json    # 用户设置
    ├── vpngate.log      # 日志（超 5MB 自动轮转）
    ├── cmd.json         # CLI → 后台命令通道
    ├── cmd_result.json  # 后台 → CLI 结果通道
    └── configs/         # OpenVPN 配置文件缓存
```

## 设计说明

### 流量隔离
代理服务器通过 `SO_BINDTODEVICE` 将所有出站连接绑定到 `tun0`。若 `tun0` 不存在（VPN 断开），代理直接返回 502，**绝不回落到真实 IP**，避免 IP 泄露。

### Watchdog 重连退避
OpenVPN 进程意外退出后，Watchdog 采用指数退避策略重连（30s → 60s → 120s → ... → 最大 10 分钟），避免因节点不可用导致的连接风暴。

### 命令总线
`vpngw` CLI 通过文件（`cmd.json` / `cmd_result.json`）与后台进程通信，无需 socket 或 IPC，兼容性强且简单可靠。

### 节点筛选逻辑
1. TCP 延迟测试（并发 50 线程）
2. OpenVPN 握手验证（并发 5 线程，避免资源耗尽）
3. 优先连接 `probe_status=available` 的节点
4. 相同优先级按延迟升序排列

## 常见问题

**Q: 安装后 `vpngw status` 显示未连接？**  
A: 首次启动需要几分钟抓取和测试节点。运行 `vpngw logs` 查看进度。

**Q: OpenVPN AUTH_FAILED 是什么意思？**  
A: 部分 VPNGate 节点要求特定凭证，系统会自动跳过这类节点选下一个。

**Q: 如何只用延迟 < 100ms 的节点？**  
A: 当前版本按延迟排序自动选择最低延迟节点，`top_nodes` 设置决定候选池大小。

**Q: 可以同时给多个 Xray 出站使用吗？**  
A: 代理服务器支持高并发（256 连接队列），完全可以。

## 依赖

- Linux（Ubuntu 20.04+/Debian 10+）
- Python 3.8+
- OpenVPN 2.4+
- iproute2、curl、git
