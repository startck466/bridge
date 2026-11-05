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
| `rule/media.yaml` | 47 | PROXY |
| `rule/cloud.yaml` | 44 | PROXY |
| `rule/apple.yaml` | 12 | DIRECT |
| `rule/cn.yaml` | 109940 | DIRECT |

合计 110264 条。`cn` 走全量兜底，分类层负责精细判定，修正层拥有最高否决权。

## 怎么用

**方式一：订阅（推荐）**

订阅地址由维护者提供（含节点信息，不放在公开仓库）。客户端每天自动拉取规则更新。

**方式二：手动接入自己的配置**

把 `dist/flclash.yaml` 里的 `rule-providers` 段复制到你自己的配置文件，`user` 替换成这个仓库的 owner：

```yaml
rule-providers:
  cn:
    type: http
    behavior: domain
    format: yaml
    url: "https://raw.githubusercontent.com/<OWNER>/bridge/main/rule/cn.yaml"
    path: ./ruleset/cn.yaml
    interval: 86400
```

然后在 `rules:` 里按顺序引用，顺序不能变：

```yaml
rules:
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
├── dist/
│   └── flclash.yaml      订阅配置骨架（不含节点）
└── scripts/
    ├── build.py          构建脚本
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
        └── direct.txt    国内全量域名（公共库清洗后）
```

## 怎么改

**改规则不要动 `rule/` 下的文件**，那是构建产物。改 `scripts/src/` 里对应分类的 txt，然后：

```bash
python3 scripts/build.py
```

**遇到网站判错**，加进 `scripts/src/fix-direct.txt` 或 `fix-proxy.txt`，注明原因和日期，重新构建。

## 维护

每周一次：

1. 更新 `scripts/data/direct.txt`（公共库最新版）
2. 复查修正层是否还有效（上游修好了就能删）
3. 重新构建，提交
4. 打 tag，例如 `v1.4`

版本号：主版本表示逻辑或分类体系变更，次版本表示域名增删。

## 免责

规则按实际使用体验持续调整，不保证覆盖所有场景。判错欢迎反馈。
