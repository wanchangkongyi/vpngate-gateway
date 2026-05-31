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

## 交互菜单

```
vg
```

```
 ╔══════════════════════════════════════════════════╗
 ║          VPNGate Gateway 管理菜单                ║
 ╠══════════════════════════════════════════════════╣
 ║  0.  修改设置                                    ║
 ║  1.  查看状态（含出口 IP 检测）                  ║
 ║  2.  抓取节点                                    ║
 ║  3.  查看节点列表                                ║
 ║  4.  自动连接最快节点                            ║
 ║  5.  按国家连接                                  ║
 ║  6.  轮换节点                                    ║
 ║  7.  手动选择节点                                ║
 ║  8.  断开连接                                    ║
 ║  9.  查看日志                                    ║
 ║  10. 服务管理（重启/停止/启动）                  ║
 ║  11. 卸载                                        ║
 ║  00. 退出                                        ║
 ╚══════════════════════════════════════════════════╝
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

`vpngw` 与 `vg` 完全等效，两个命令都可以用。

## 设置项

在交互菜单选 `0` 或使用 `vg set` 命令修改：

| 序号 | 设置项 | 默认值 | 说明 |
|------|--------|--------|------|
| 1 | `country_filter` | 不限 | 国家白名单，如 `JP,US,KR`，空=不限 |
| 2 | `rotate_hours` | `2` | 自动轮换间隔（小时），`0`=关闭 |
| 3 | `top_nodes` | `20` | 保留延迟最低的前 N 个节点 |
| 4 | `probe_count` | `10` | OpenVPN 握手验证节点数，`0`=跳过验证 |
| 5 | `proxy_port` | `7928` | 代理监听端口 |
| 6 | `auto_fetch_on_start` | `true` | 服务启动时自动抓取节点 |

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

支持两种卸载方式：
- **卸载服务**：删除服务和程序文件，保留 openvpn 等依赖，重新安装时无需重装依赖
- **彻底卸载**：同时移除 openvpn、iproute2 等依赖包

## 服务管理

```bash
vg restart                          # 重启服务
systemctl status vpngate-gateway    # 查看服务状态
journalctl -u vpngate-gateway -f    # 实时查看系统日志
```

## 常见问题

**节点列表探测显示 ✘ 但能正常连接？**
正常现象。VPNGate 节点的 OpenVPN 握手验证在没有 VPN 的环境下容易被防火墙拦截，不代表节点不可用。可以用 `vg set probe_count 0` 跳过验证，直接按延迟排序连接。

**出口 IP 和节点 IP 不一样？**
正常现象。VPNGate 节点本身是中继服务器，连接 IP 是节点的入口地址，出口 IP 是节点实际出网的地址，两者不同是正常的。

**修改端口后代理不可用？**
修改 `proxy_port` 后需要重启服务生效：`vg restart`，同时记得同步更新 Xray/3x-ui 里的端口配置。

**512M 内存的机器能用吗？**
可以。正常运行约占 150MB。首次抓取节点时内存会短暂升高，建议设置 `probe_count 5` 和 `top_nodes 15` 降低内存压力。

## 依赖

- Linux（Ubuntu 20.04+ / Debian 10+）
- Python 3.8+
- OpenVPN 2.4+
- curl、iproute2
