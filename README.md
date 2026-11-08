# bridge

**给任何 VPN / 代理客户端用的分流规则订阅。不含任何节点、密码、UUID 或订阅令牌——纯规则，随便取用。**

> 这份规则的核心用途是**定向让 AI 服务走代理**：ChatGPT、Claude、Gemini、Grok、Perplexity、Cursor、HuggingFace 这些站，在国内要么直连不通、要么被降智或风控，bridge 把 63 个 AI 域名单独归成一层，命中就走代理，不受其它分类干扰。同时它也解决国内站被误判绕道的问题。
>
> **由卡戎（AI 小黑猫）维护。** 每周自动重建一次，域名清单持续增补。

分流规则库。内核无关的部分是规则本身（域名清单），可以喂给 mihomo 系（FlClash / Clash Verge / Stash）、sing-box、Surge、Shadowrocket，或者直接当纯域名清单用。

**最省事的一条**（mihomo 系整份配置，含 cn 全量；导入后只需填自己的节点）：

```
https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/clash-full.yaml
```

不是 geosite 的镜像，是**判定逻辑 + 可随时打补丁的修正层**。

| 我要… | 去哪 |
|---|---|
| **直接拿现成的规则订阅链接，导入就行** | [→ 方式 1：`clash-full.yaml`](#方式-1整份配置最省事推荐这个) |
| 客户端内存紧张，不想要 11 万条 cn | [→ 方式 1B：精简版](#方式-1b精简版不带-cn靠-geoip-兜底) |
| 看规则怎么判断的 | [RULES.md](RULES.md) |
| 只要 AI 走代理的最小配置 | [方式 3：只用 AI 那一层](#方式-3只用-ai-那一层) |
| 要带我节点的完整配置 | 那是私有的一份，见 [订阅（含节点，加密托管）](#订阅含节点加密托管)，不在公开仓库 |

## 它解决什么问题

公共规则库判断的是「域名属于哪个国家」，但实际用起来真正难受的是三件事：

- 国内网站买了海外 CDN，被判成国外，绕一圈代理回来反而更慢
- 该走代理的国外服务，因为国内有节点被判成直连，打不开
- 冷门站、新站公共库没收，全靠 IP 归属地猜，最容易出错

bridge 的做法是：基础池保证不漏，**修正层保证不错**。任何一条判错都能被单独纠正，不影响其他。

详细判断逻辑见 [RULES.md](RULES.md)。

## 规则订阅（不含节点）

**这一节是给所有人的——里面没有任何节点。** 你只需要自己准备代理客户端和节点，规则用这里的。

所有链接都走 jsDelivr 的 `fastly` 子域（国内可直连）。每 24 小时自动更新一次，你不用管。

### 方式 1：整份配置（最省事，推荐这个）

mihomo 内核通用（FlClash / Clash Verge / Clash Meta / Stash）：

```
https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/clash-full.yaml
```

这就是**默认推荐**的一份：修正层 + 六个分类层 + `cn` 全量直连，**11 万条国内域名按域名级判直连**，不靠 IP 归属地猜，也不依赖客户端自带的 geosite。国内站被误判绕道、AI 站被判直连打不开，这两类问题它都能兜住。

导入后**必须做一步**：填你自己的节点。文件里 `proxies: []` 是空的，注释里给了两种填法（改 `proxy-providers` 指向你的机场订阅，或者手动写节点）。不填就是不代理，规则照常加载但不生效——这是刻意设计，不会静默走错。

规则顺序是 `fix-direct` → `apple` → 六个 PROXY 分类 → **`cn,DIRECT`** → `GEOIP,CN` → `MATCH`。`cn` 明确排在所有 `PROXY` 规则之后、`GEOIP,CN` 之前，所以 AI 那一层永远优先。

唯一的代价：首次启动多下一次 `rule/cn.yaml`（约 2.2MB），之后每 24 小时增量更新，多占十几 MB 内存。规则本身走远程 `rule-provider`，不写进模板文件，所以模板和精简版一样轻。

### 方式 1B：精简版（不带 cn，靠 GEOIP 兜底）

同样是整份配置，但不挂 `cn` 规则集，只靠客户端自带的 `GEOIP,CN` 判断国内流量：

```
https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/clash.yaml
```

适合这几种情况：

- 客户端内存紧张（路由器、旧手机），十几 MB 的域名表确实吃不消
- 客户端的 `GEOIP` 数据库很新，国内 IP 段覆盖完整
- 只想先试试规则效果，不想一次拉 2.2MB

代价是：`GEOIP,CN` 只能按 IP 归属地判，国内站用了海外 CDN 时仍可能被判成国外、绕一圈代理——这正是完整版用域名规则钉死的那类问题。

> **`cn` 只出现在直连侧。** 11 万条国内域名如果混进代理集，等于把国内站全推去代理——正好和这个项目的用途相反。所以 `singbox-direct-full.json` / `list-direct-full.txt` / `domains-direct-full.txt` 都是**直连**集，代理集继续用精简版那几个文件。

### 方式 2：只取规则片段，塞进你现有配置

已经有配置、只想加规则的话，复制 [`export/providers-full.yaml`](export/providers-full.yaml) 的两段合并进去（不想带 cn 全量就用精简版 [`export/providers.yaml`](export/providers.yaml)）：

```yaml
rule-providers:
  ai:
    type: http
    behavior: domain
    format: yaml
    url: "https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule/ai.yaml"
    path: ./ruleset/bridge-ai.yaml
    interval: 86400
  # ... 其余七个同理

rules:
  - RULE-SET,fix-direct,DIRECT    # 修正层，必须最前
  - RULE-SET,fix-proxy,PROXY
  - RULE-SET,ai,PROXY             # ← AI 走代理就在这一行
  - RULE-SET,dev,PROXY
  - RULE-SET,social,PROXY
  - RULE-SET,media,PROXY
  - RULE-SET,cloud,PROXY
  - RULE-SET,apple,DIRECT
  - GEOIP,CN,DIRECT
  - MATCH,PROXY
```

**顺序不能变**，`fix-direct` 必须在最前（它有否决权）。

### 方式 3：只用 AI 那一层

只想要「AI 走代理、其它别管」的最小配置：

```yaml
rule-providers:
  ai:
    type: http
    behavior: domain
    format: yaml
    url: "https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule/ai.yaml"
    path: ./ruleset/bridge-ai.yaml
    interval: 86400

rules:
  - RULE-SET,ai,PROXY
  - GEOIP,CN,DIRECT
  - MATCH,DIRECT
```

63 个 AI 域名，覆盖各家大模型、AI 编程、AI 绘图/音频/视频、模型托管。

### 方式 4：其它软件

| 软件 | 用哪个文件 | 说明 |
|---|---|---|
| **sing-box** | [`singbox-proxy.json`](export/singbox-proxy.json) · [`singbox-direct.json`](export/singbox-direct.json) | 标准 `rule_set`（version 3，`domain_suffix`），`format: "source"` 直接引用 |
| **Surge / Shadowrocket / Stash** | [`list.txt`](export/list.txt) · [`list-direct.txt`](export/list-direct.txt) | `DOMAIN-SUFFIX` 行，用 `RULE-SET,<url>,<你的策略组>` 引用 |
| **Clash（classical）** | 同上 `list.txt` | `behavior: classical` |
| **任意其它** | [`domains.txt`](export/domains.txt) · [`domains-direct.txt`](export/domains-direct.txt) | 每行一个域名，喂给谁都能用 |
| **v2ray / Xray 系** | [`domains.txt`](export/domains.txt) | 转成 `geosite` 格式或写进 `routing.rules[].domain` 的 `domain:` 数组 |
| **上面这些建议配 cn 全量** | `*-direct-full` 三个文件 | 只扩直连侧，约 11 万条，文件 1.4～3MB |

sing-box 示例（推荐组合：代理用精简集，直连用含 cn 全量的那份）：

```json
{
  "rule_set": [
    {
      "type": "remote",
      "tag": "bridge-proxy",
      "format": "source",
      "url": "https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/singbox-proxy.json",
      "download_detour": "proxy",
      "update_interval": "24h"
    },
    {
      "type": "remote",
      "tag": "bridge-cn",
      "format": "source",
      "url": "https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/singbox-direct-full.json",
      "download_detour": "proxy",
      "update_interval": "24h"
    }
  ],
  "rules": [
    { "rule_set": "bridge-proxy", "outbound": "proxy" },
    { "rule_set": "bridge-cn", "outbound": "direct" }
  ]
}
```

**顺序不能反**：`bridge-proxy` 在前，`bridge-cn` 在后。反了就是把 AI 站也判成直连。

Surge 示例（同样两条，代理精简 + 直连全量）：

```
RULE-SET,https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/list.txt,PROXY
RULE-SET,https://fastly.jsdelivr.net/gh/startck466/bridge@main/export/list-direct-full.txt,DIRECT
```

> `PROXY` 换成你自己配置里的策略组名。

### 导出文件一览

`export/` 下全部由 `scripts/export.py` 生成，**只有域名，无任何凭据**：

| 文件 | 内容 | 条数 |
|---|---|---|
| `clash-full.yaml` | **推荐**：mihomo 完整配置模板，含 `cn` 全量（节点留空） | — |
| `providers-full.yaml` | 可粘贴片段，含 `cn` 全量 | — |
| `singbox-direct-full.json` | sing-box 直连集，含 cn 全量 | 109961 |
| `list-direct-full.txt` | `DOMAIN-SUFFIX` 直连集，含 cn 全量 | 109961 |
| `domains-direct-full.txt` | 纯域名直连集，含 cn 全量 | 109961 |
| `clash.yaml` | 精简版：mihomo 完整配置模板（节点留空） | — |
| `providers.yaml` | 精简版：可粘贴的 rule-providers + rules 片段 | — |
| `singbox-proxy.json` | sing-box `rule_set`，走代理 | 281 |
| `singbox-direct.json` | sing-box `rule_set`，强制直连 | 42 |
| `list.txt` | `DOMAIN-SUFFIX` 列表，走代理 | 281 |
| `list-direct.txt` | `DOMAIN-SUFFIX` 列表，强制直连 | 42 |
| `domains.txt` | 纯域名，走代理 | 281 |
| `domains-direct.txt` | 纯域名，强制直连 | 42 |

**完整版 vs 精简版**：完整版额外把 `cn` 的 11 万条域名给你，完全不依赖 `GEOIP` 与 geosite，代价是多占十几 MB 内存和一次 2.2MB 下载——**默认建议选它**。精简版只导出「公共库判不对」的部分（修正层 + 分类层，共 323 条），靠客户端自带的 `GEOIP,CN` 兜底国内流量，加载快、内存小；只有客户端内存实在紧张时才选。

**注意**：代理侧永远用精简版的 `singbox-proxy.json` / `list.txt` / `domains.txt`，完整版的三个 `*-direct-full` 全是直连集。别把 11 万条 cn 塞进代理侧。

`clash.yaml` 与 `clash-full.yaml` 里的 `cn` 都是远程 `rule-provider` 引用，不写进模板文件本身——所以两份模板体积几乎一样，只有规则判定多一层。

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

## 订阅（含节点，不进仓库）

带节点的完整配置**不再经过 GitHub**，也不再有加密密文入库。仓库只承载规则，
订阅由服务器直接下发：

```
主通道  https://startck466.us.ci:8880/s/<64 位路径令牌>     # 直连，Let's Encrypt 证书
救援通道 https://s.startck466.us.ci/s/<64 位路径令牌>        # Cloudflare 橙云回源
```

| 环节 | 做法 |
|---|---|
| 生成 | 本地 `bridge-private/make_sub.py`，节点来自 `scripts/.hosts.json`（gitignore） |
| 下发 | `bridge-private/publish_sub.py` 上传到 ZGO `/root/bridge-sub/bridge-sub.yaml`（600） |
| 服务 | `bridge-sub.service`：8880 TLS + 80 明文（只给 CF 回源），只响应 `/s/<令牌>` |
| 令牌 | 32 字节随机 → 64 位 hex，存服务器 `/root/bridge-sub/token`（600），常量时间比较 |
| 传输 | 主通道端到端 TLS；CF 通道边缘 HTTPS + 回源 HTTP（Flexible） |

为什么留两条：服务器 IP 若在内地侧被封，主通道立刻失效，但 CF 的回源是从境外
边缘发起的，仍能把最新订阅取回来——这是换节点、换 IP 时的救援路径，不是日常首选。
CF 的 HTTPS 回源端口只支持 443/2053/2083/2087/2096/8443，8880 只能走 HTTP 回源，
所以源站额外开了一个 80 明文监听，仅由 CF 使用。

改节点后重新下发：

```bash
python3 ../bridge-private/publish_sub.py
```

> 订阅 URL 本质是能力 URL，拿到链接的人就能用。链接不要发群里、不要贴公开页面。

## 分发方式

规则集通过 **jsDelivr** 分发，国内可直连，无需任何自建服务：

```
https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule/<名称>.yaml
```

> 注意：`cdn.jsdelivr.net` 与 `raw.githubusercontent.com` 在国内均被 DNS 污染，必须用 `fastly` / `gcore` / `testingcf` 子域。当前使用 `fastly`，失效时换另外两个即可。

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
├── export/               给其它客户端的规则订阅（纯域名，无凭据）
│   ├── clash.yaml
│   ├── providers.yaml
│   ├── singbox-proxy.json
│   ├── singbox-direct.json
│   ├── list.txt
│   ├── list-direct.txt
│   ├── domains.txt
│   ├── domains-direct.txt
│   ├── clash-full.yaml            完整版：多挂 cn 规则集
│   ├── providers-full.yaml
│   ├── singbox-direct-full.json   含 cn 全量
│   ├── list-direct-full.txt
│   └── domains-direct-full.txt
└── scripts/
    ├── build.py          构建脚本：src/*.txt → rule/*.yaml
    ├── export.py         导出通用规则订阅 → export/
    ├── weekly.py         每周维护：拉上游 → 构建 → 导出 → 推送 → 定 ref → 下发订阅 → 校验
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

它做七件事：

1. 从 jsDelivr 拉上游最新国内域名清单（多通道依次重试，行数骤降 20% 以上自动保留旧数据）
2. 重新构建全部规则集（按优先级跨分类去重）
3. 导出 `export/` 下的通用规则订阅（纯域名，无节点；精简版 + 含 cn 全量的完整版）
4. 提交并推送到本仓库
5. 把客户端的规则地址固定到本次提交（`scripts/sub-ref.txt`），避免 `@main` 边缘缓存读到旧内容
6. 调用私有脚本把含节点的订阅下发到自有服务器（脚本不在本仓库；缺失时这一步跳过并提示）
7. 刷新 jsDelivr 边缘缓存，逐个校验九个规则集返回 HTTP 200 且内容正确

加 `--no-push` 只本地重建，不推 GitHub。

凭据放 `scripts/.hosts.json`，已被 `.gitignore` 排除，**不得入库**。

## 维护者

规则由 **卡戎（AI 小黑猫）** 每周自动维护：拉上游、重建、校验、推送全程无人值守。

判错、漏收、想加的站点，开 issue 说清楚「哪个域名 + 该直连还是该代理 + 为什么会判错」，下一轮就能进修正层。

> 卡戎是柯一第一只猫的名字——一只黑色孟买猫，家里的老大。这个名字有意义，所以认真做事。

## 免责

规则按实际使用体验持续调整，不保证覆盖所有场景。判错欢迎反馈。

本仓库**不含任何节点、密码、UUID、订阅令牌或服务器信息**。所有规则是公开可查的域名清单，仅供分流判断使用。请自备节点。
