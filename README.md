# VPNGate Gateway

将 [VPNGate](https://www.vpngate.net) 免费公共节点作为 VPS 出口 IP，本地暴露 SOCKS5/HTTP 双协议代理供 Xray/3x-ui 或其他工具使用。出口 IP 每隔一段时间自动轮换，SSH 管理流量走 eth0 物理网卡，**不会因切换 VPN 而断连**。

## 架构

```
Xray / 3x-ui
    │ SOCKS5 出站
    ▼
127.0.0.1:7928  ← HTTP/SOCKS5 双协议代理
    │ SO_BINDTODEVICE → tun0（VPN 断开时直接返回 502，不泄露真实 IP）
    ▼
tun0  ← OpenVPN 虚拟网卡
    │ 策略路由表 100
    ▼
VPNGate 公共节点（日本 / 美国 / 韩国 / ...）
    │
    ▼
目标网站（看到 VPNGate 节点 IP，非 VPS 真实 IP）
```

## 快速安装

```bash
bash <(curl -Ls https://raw.githubusercontent.com/wanchangkongyi/vpngate-gateway/main/install.sh)
```

安装完成后直接输入 `vg` 打开交互菜单。

```

## 命令参考

| 命令 | 说明 |
|------|------|
| `vg` | 打开交互菜单 |
| `vg status` | 查看状态 |
| `vg status --check` | 查看状态并实时检测出口 IP |
| `vg fetch` | 抓取全部节点并测速 |
| `vg fetch --country JP,US` | 只抓取日本/美国节点 |
| `vg fetch --top 30` | 保留前 30 个节点 |
| `vg auto` | 自动连接最快节点 |
| `vg auto JP` | 自动连接最快日本节点 |
| `vg nodes` | 查看节点列表 |
| `vg nodes --country JP` | 只看日本节点 |
| `vg connect <node_id>` | 连接指定节点 |
| `vg rotate` | 切换到下一个节点 |
| `vg stop` | 断开 VPN 连接 |
| `vg logs` | 查看最近 50 行日志 |
| `vg logs -n 100` | 查看最近 100 行日志 |
| `vg restart` | 重启服务 |
| `vg set <key> <value>` | 修改设置 |
| `vg uninstall` | 卸载 |

```

示例：

```bash
vg set rotate_hours 1        # 每1小时轮换一次
vg set country_filter JP,KR  # 只用日本/韩国节点
vg set probe_count 0         # 跳过握手验证，只用延迟排序
vg set top_nodes 30          # 保留前30个节点
```

## Xray / 3x-ui 出站配置

**SOCKS5：**

```json
{
  "tag": "vpngate-out",
  "protocol": "socks",
  "settings": {
    "servers": [{ "address": "127.0.0.1", "port": 7928 }]
  }
}
```

**HTTP：**

```json
{
  "tag": "vpngate-out",
  "protocol": "http",
  "settings": {
    "servers": [{ "address": "127.0.0.1", "port": 7928 }]
  }
}
```

其他工具使用代理地址：
- SOCKS5：`socks5://127.0.0.1:7928`
- HTTP：`http://127.0.0.1:7928`

## 卸载

```bash
vg uninstall
```

## 依赖

- Linux（Ubuntu 20.04+ / Debian 10+）
- Python 3.8+
- OpenVPN 2.4+
- curl、iproute2
