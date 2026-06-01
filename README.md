# VPNGate Gateway

将 [VPNGate](https://www.vpngate.net) 免费公共节点作为 VPS 出口 IP，本地暴露 SOCKS5/HTTP 双协议代理供 Xray/3x-ui 或其他工具使用。出口 IP 定期自动轮换，SSH 管理流量走 eth0 物理网卡，**不会因切换 VPN 而断连**。


## 快速安装

```bash
bash <(curl -Ls https://raw.githubusercontent.com/wanchangkongyi/vpngate-gateway/main/install.sh)
```

安装完成后输入 `vg` 打开交互菜单。

## 交互菜单

```
 ╔══════════════════════════════════════════════════╗
 ║          VPNGate Gateway 管理菜单                ║
 ╠══════════════════════════════════════════════════╣
 ║  0.  修改设置                                    ║
 ║  1.  查看状态                                    ║
 ║  2.  抓取节点                                    ║
 ║  3.  查看节点列表                                ║
 ║  4.  自动连接最快节点                            ║
 ║  5.  检测节点延迟                                ║
 ║  6.  轮换节点                                    ║
 ║  7.  手动选择节点                                ║
 ║  8.  断开连接                                    ║
 ║  9.  查看日志                                    ║
 ║  10. 服务管理                                    ║
 ║  11. 卸载                                        ║
 ║  00. 退出                                        ║
 ╚══════════════════════════════════════════════════╝
```


## 设置项

在交互菜单选 `0` 或使用 `vg set` 命令修改：

| 序号 | 设置项 | 默认值 | 说明 |
|------|--------|--------|------|
| 1 | `country_filter` | 不限 | 国家白名单，如 `JP,US,KR`，空=不限 |
| 2 | `rotate_hours` | `2` | 自动轮换间隔（小时），`0`=关闭 |
| 3 | `latency_check_hours` | `1` | 定时延迟检测间隔（小时），`0`=关闭 |
| 4 | `min_nodes` | `5` | 节点数低于此值时自动重新抓取 |
| 5 | `top_nodes` | `20` | 抓取后保留前 N 个节点 |
| 6 | `probe_count` | `10` | OpenVPN 握手验证节点数，`0`=跳过 |
| 7 | `proxy_port` | `7928` | 代理监听端口 |
| 8 | `auto_fetch_on_start` | `true` | 服务启动时自动抓取节点 |

示例：

```bash
vg set rotate_hours 1          # 每1小时轮换一次
vg set country_filter JP,KR    # 只用日本/韩国节点
vg set latency_check_hours 2   # 每2小时检测一次延迟
vg set min_nodes 3             # 节点数低于3个时自动重抓
vg set probe_count 0           # 跳过握手验证，只用延迟排序
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

其他工具：
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
