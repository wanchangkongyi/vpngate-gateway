# VPNGate Gateway

将 VPNGate 免费公共节点作为 VPS 出口 IP，本地暴露 SOCKS5 代理供 Xray/3x-ui 使用。

## 架构

```
Xray/3x-ui
    │ SOCKS5 出站
    ▼
127.0.0.1:7928 (microsocks，绑定 tun0)
    │ SO_BINDTODEVICE
    ▼
tun0 虚拟网卡
    │ 策略路由表 100
    ▼
OpenVPN 加密隧道
    │
    ▼
VPNGate 节点（日本/美国/等）
    │
    ▼
目标网站（看到 VPNGate 节点 IP）
```

**SSH 管理流量走 eth0 物理网卡，不受影响，不会断连。**

## 安装

```bash
bash <(curl -Ls https://raw.githubusercontent.com/wanchangkongyi/vpngate-gateway/main/install.sh)
```

## 命令

| 命令 | 说明 |
|------|------|
| `vpngw` | 打开交互菜单 |
| `vpngw status` | 查看当前状态和出口 IP |
| `vpngw fetch` | 抓取所有节点并测速 |
| `vpngw fetch --country JP,US` | 只抓取日本/美国节点 |
| `vpngw fetch --top 30` | 保留前30个节点 |
| `vpngw auto` | 自动连接最快节点 |
| `vpngw auto JP` | 自动连接最快日本节点 |
| `vpngw nodes` | 查看节点列表 |
| `vpngw rotate` | 切换下一个节点 |
| `vpngw stop` | 停止服务 |

## Xray/3x-ui 出站配置

在 3x-ui 添加出站代理：

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

## 自动轮换

安装后默认每 **2 小时**自动切换一个节点，出口 IP 持续变化。

修改轮换间隔：
```bash
# 改为每1小时
systemctl edit vpngate-rotate.timer
# 添加：
# [Timer]
# OnUnitActiveSec=1h
```

## 节点选择模式

| 模式 | 命令 |
|------|------|
| 自动最快 | `vpngw auto` |
| 指定国家 | `vpngw auto JP` |
| 手动选择 | `vpngw menu` → 选7 |
| 自动轮换 | 默认每2小时自动执行 |
