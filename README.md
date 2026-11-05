# bridge

分流规则库。给 mihomo 内核的客户端用（FlClash / Stash / Clash Verge）。

不是 geosite 的镜像，是**判定逻辑 + 可随时打补丁的修正层**。

## 它解决什么问题

公共规则库判断的是「域名属于哪个国家」，但实际用起来真正难受的是三件事：

- 国内网站买了海外 CDN，被判成国外，绕一圈代理回来反而更慢
- 该走代理的国外服务，因为国内有节点被判成直连，打不开
- 冷门站、新站公共库没收，全靠 IP 归属地猜，最容易出错

bridge 的做法是：基础池保证不漏，**修正层保证不错**。任何一条判错都能被单独纠正，不影响其他。

详细判断逻辑见 [RULES.md](RULES.md)。

## 规则集

| 文件 | 条数 | 去向 |
|---|---|---|
| `rule/fix-direct.yaml` | 30 | DIRECT |
| `rule/fix-proxy.yaml` | 0 | PROXY |
| `rule/ai.yaml` | 63 | PROXY |
| `rule/dev.yaml` | 82 | PROXY |
| `rule/social.yaml` | 46 | PROXY |
| `rule/media.yaml` | 46 | PROXY |
| `rule/cloud.yaml` | 44 | PROXY |
| `rule/apple.yaml` | 12 | DIRECT |
| `rule/cn.yaml` | 109919 | DIRECT |

合计 110242 条。`cn` 走全量兜底，分类层负责精细判定，修正层拥有最高否决权。

## 怎么用

规则集通过 **jsDelivr** 分发，国内可直连，无需任何自建服务：

```
https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule/<名称>.yaml
```

> 注意：`cdn.jsdelivr.net` 与 `raw.githubusercontent.com` 在国内均被 DNS 污染，必须用 `fastly` / `gcore` / `testingcf` 子域。当前使用 `fastly`，失效时换另外两个即可。

把下面这段放进你自己的配置，节点由你自己填：

```yaml
rule-providers:
  fix-direct:
    type: http
    behavior: domain
    format: yaml
    url: "https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule/fix-direct.yaml"
    path: ./ruleset/fix-direct.yaml
    interval: 86400
  # ... 其余八个同理，文件名替换即可

rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - IP-CIDR6,fc00::/7,DIRECT,no-resolve
  - IP-CIDR6,fe80::/10,DIRECT,no-resolve
  - RULE-SET,fix-direct,DIRECT
  - RULE-SET,fix-proxy,PROXY
  - RULE-SET,ai,PROXY
  - RULE-SET,dev,PROXY
  - RULE-SET,social,PROXY
  - RULE-SET,media,PROXY
  - RULE-SET,cloud,PROXY
  - RULE-SET,apple,DIRECT
  - RULE-SET,cn,DIRECT
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
```

顺序不能变，`fix-*` 必须最前，`cn` 必须在最后。

`interval: 86400` 表示客户端每 24 小时自动刷新一次规则，**不需要任何手动操作**。拉取失败时沿用本地缓存，不会导致配置失效。

## 目录结构

```
bridge/
├── RULES.md              判断逻辑（改规则前先看这个）
├── rule/                 生成的规则集，勿手工编辑
│   ├── fix-direct.yaml
│   ├── fix-proxy.yaml
│   ├── ai.yaml
│   ├── dev.yaml
│   ├── social.yaml
│   ├── media.yaml
│   ├── cloud.yaml
│   ├── apple.yaml
│   └── cn.yaml
└── scripts/
    ├── build.py          构建脚本：src/*.txt → rule/*.yaml
    ├── make_sub.py       生成含节点的订阅配置（节点信息不入库）
    ├── weekly.py         每周维护：拉上游 → 构建 → 推送 → 刷缓存 → 校验
    ├── src/              手工分类清单 ← 改这里
    │   ├── fix-direct.txt
    │   ├── fix-proxy.txt
    │   ├── ai.txt
    │   ├── dev.txt
    │   ├── social.txt
    │   ├── media.txt
    │   ├── cloud.txt
    │   └── apple.txt
    └── data/
        └── direct.txt    国内全量域名（每周下载，不入库）
```

## 怎么改

**改规则不要动 `rule/` 下的文件**，那是构建产物。改 `scripts/src/` 里对应分类的 txt，然后：

```bash
python3 scripts/build.py
```

**遇到网站判错**，加进 `scripts/src/fix-direct.txt` 或 `fix-proxy.txt`，注明原因和日期，重新构建。

## 维护

每周一次，一条命令：

```bash
python3 scripts/weekly.py
```

它做四件事：

1. 从 jsDelivr 拉上游最新国内域名清单（多通道依次重试，行数骤降 20% 以上自动保留旧数据）
2. 重新构建全部规则集（按优先级跨分类去重）
3. 提交并推送到本仓库
4. 刷新 jsDelivr 边缘缓存，逐个校验九个规则集返回 HTTP 200 且内容正确

加 `--no-push` 只本地重建，不推 GitHub。

凭据放 `scripts/.hosts.json`，已被 `.gitignore` 排除，**不得入库**。

## 免责

规则按实际使用体验持续调整，不保证覆盖所有场景。判错欢迎反馈。
