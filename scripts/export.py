#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge 通用规则导出 —— 给「其他客户端」用的规则订阅

只导出**域名规则**，不含任何节点、密码、UUID、订阅令牌。
产物在 export/ 目录，全部可公开分发。

用法:
    python3 scripts/export.py

输出（精简版：只含「公共库判不对」的部分）:
    export/clash.yaml          mihomo 系（FlClash / Clash Verge / Stash）完整模板
    export/providers.yaml      可粘贴片段：只含 rule-providers + rules 顺序
    export/singbox-proxy.json  sing-box：该走代理的全部域名
    export/singbox-direct.json sing-box：该强制直连的域名（修正层）
    export/list.txt            Surge / Shadowrocket / Stash：DOMAIN-SUFFIX（代理）
    export/list-direct.txt     同上（强制直连）
    export/domains.txt         纯域名清单（该走代理），任何软件都能吃
    export/domains-direct.txt  纯域名清单（该强制直连）

输出（完整版：额外含 cn 全量 11 万条，不依赖 GEOIP 也能判国内）:
    export/clash-full.yaml            mihomo 完整模板 + cn rule-provider
    export/providers-full.yaml        片段版 + cn rule-provider
    export/singbox-direct-full.json   sing-box 直连集 = 修正层 + apple + cn 全量
    export/list-direct-full.txt       classical 直连集全量
    export/domains-direct-full.txt    纯域名直连集全量

设计取舍:
    - 精简版不导出 cn 全量：第三方客户端已有 GEOIP,CN 与公共库兜底，
      塞进去只会拖慢解析。完整版给「想彻底不依赖 GEOIP」的人用。
    - cn 在完整版里只并入**直连**侧。它属于兜底集，绝不能进代理集——
      否则 11 万条国内域名会被判去代理，正好和本项目的用途相反。
    - 顺序即语义：fix-* 是修正层，必须排在任何分类之前；
      cn 排在全部代理分类之后、GEOIP,CN 之前（当前 cn 与代理分类零重叠，
      构建时已按优先级剔重，放后面是双保险）。
"""

import io
import json
import os
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE = os.path.join(BASE, 'rule')
OUT = os.path.join(BASE, 'export')

REPO = 'startck466/bridge'
CDN = 'https://fastly.jsdelivr.net/gh/%s@main/rule' % REPO
RAW_BASE = 'https://fastly.jsdelivr.net/gh/%s@main/export' % REPO

# 走代理的分类，顺序即匹配优先级
PROXY_CATS = ['fix-proxy', 'ai', 'dev', 'social', 'media', 'cloud']
# 强制直连的分类（修正层 + 苹果）
DIRECT_CATS = ['fix-direct', 'apple']

SIGN = '由卡戎（AI 小黑猫）维护'


def parse_ruleset(name):
    """从 rule/<name>.yaml 取出域名列表（去掉 '+.' 前缀）"""
    path = os.path.join(RULE, name + '.yaml')
    out = []
    if not os.path.isfile(path):
        return out
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line.startswith("- '"):
                continue
            v = line[3:].rstrip("'").strip()
            if v.startswith('+.'):
                v = v[2:]
            if v:
                out.append(v)
    return out


def load_all():
    data = {}
    for name in PROXY_CATS + DIRECT_CATS:
        data[name] = parse_ruleset(name)
    return data


def uniq(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------- clash 完整模板
def write_clash(data, today, full=False):
    name_out = 'clash-full.yaml' if full else 'clash.yaml'
    proxies_lines = []
    for name in PROXY_CATS:
        target = 'PROXY'
        proxies_lines.append('  %s:' % name)
        proxies_lines.append('    type: http')
        proxies_lines.append('    behavior: domain')
        proxies_lines.append('    format: yaml')
        proxies_lines.append('    url: "%s/%s.yaml"' % (CDN, name))
        proxies_lines.append('    path: ./ruleset/%s.yaml' % name)
        proxies_lines.append('    interval: 86400')

    out = []
    out.append('# ============================================================')
    out.append('# bridge —— 分流规则模板（不含任何节点）%s' % ('· 完整版（含 cn 全量）' if full else ''))
    out.append('# 生成: %s   %s' % (today, SIGN))
    out.append('# 说明: https://github.com/%s' % REPO)
    out.append('# ============================================================')
    if full:
        out.append('#')
        out.append('# 与 clash.yaml 的唯一区别：多一个 cn 规则集（约 11 万条国内域名），')
        out.append('# 域名级判定直连，不依赖 GEOIP 数据库。适合客户端没有 GEOIP、')
        out.append('# 或想完全绕开 IP 归属地判断的场景。代价是首次启动要多下一次 ~2.2MB。')
        out.append('#')
        out.append('# 想省内存/启动更快 → 用 clash.yaml（靠 GEOIP,CN 兜底）。')
    out.append('#')
    out.append('# ⚠️ 这个文件里没有节点。导入后你必须做一步：')
    out.append('#')
    out.append('#   方式 A（推荐，用你自己的机场订阅）——')
    out.append('#     1. 解除下面 proxy-providers 的注释，把 url 换成你的订阅链接')
    out.append('#     2. 解除 proxy-groups 里「use: [my-sub]」的注释')
    out.append('#')
    out.append('#   方式 B（手动填节点）——')
    out.append('#     1. 在 proxies: 下面按 mihomo 格式写入你的节点')
    out.append('#     2. 把节点名加进 proxy-groups → PROXY → proxies 列表')
    out.append('#')
    out.append('# 没填节点时：所有规则照常生效，但 PROXY 组只有 DIRECT，')
    out.append('# 等于全部直连。这是刻意的——不填就不代理，不会静默走错。')
    out.append('#')
    out.append('# 核心用途：让 AI 服务（ChatGPT / Claude / Gemini / Grok 等）')
    out.append('# 稳定走代理，同时国内站不被误判绕道。')
    out.append('')
    out.append('mixed-port: 7890')
    out.append('allow-lan: false')
    out.append('mode: rule')
    out.append('log-level: info')
    out.append('ipv6: true')
    out.append('unified-delay: true')
    out.append('tcp-concurrent: true')
    out.append('find-process-mode: off')
    out.append('global-client-fingerprint: chrome')
    out.append('')
    out.append('dns:')
    out.append('  enable: true')
    out.append('  ipv6: true')
    out.append('  enhanced-mode: fake-ip')
    out.append('  fake-ip-range: 198.18.0.1/16')
    out.append('  fake-ip-filter:')
    out.append('    - "*.lan"')
    out.append('    - "*.local"')
    out.append('  default-nameserver:')
    out.append('    - 223.5.5.5')
    out.append('    - 119.29.29.29')
    out.append('  proxy-server-nameserver:')
    out.append('    - https://dns.alidns.com/dns-query')
    out.append('    - https://doh.pub/dns-query')
    out.append('  nameserver:')
    out.append('    - system')
    out.append('    - https://dns.alidns.com/dns-query')
    out.append('    - https://doh.pub/dns-query')
    out.append('  nameserver-policy:')
    out.append('    # CN 域名走设备默认 DNS + 国内 DoH 并发抢答')
    out.append('    # （system 单用会在 TUN 客户端回环，DoH 用于兜底）')
    out.append('    "geosite:cn":')
    out.append('      - system')
    out.append('      - https://dns.alidns.com/dns-query')
    out.append('      - https://doh.pub/dns-query')
    out.append('    "geosite:geolocation-!cn":')
    out.append('      - https://1.1.1.1/dns-query')
    out.append('      - https://8.8.8.8/dns-query')
    out.append('  fallback:')
    out.append('    - https://1.1.1.1/dns-query')
    out.append('    - https://8.8.8.8/dns-query')
    out.append('  fallback-filter:')
    out.append('    geoip: true')
    out.append('    geoip-code: CN')
    out.append('')
    out.append('proxies: []   # ← 方式 B：把你的节点写在这里')
    out.append('')
    out.append('# proxy-providers:')
    out.append('#   my-sub:')
    out.append('#     type: http')
    out.append('#     url: "https://你的机场订阅链接"')
    out.append('#     interval: 3600')
    out.append('#     path: ./providers/my-sub.yaml')
    out.append('#     health-check:')
    out.append('#       enable: true')
    out.append('#       url: https://www.gstatic.com/generate_204')
    out.append('#       interval: 300')
    out.append('')
    out.append('proxy-groups:')
    out.append('  - name: "PROXY"')
    out.append('    type: select')
    out.append('    proxies:')
    out.append('      - DIRECT')
    out.append('#       - 你的节点名')
    out.append('#     use:')
    out.append('#       - my-sub')
    out.append('')
    out.append('rule-providers:')
    out.extend(proxies_lines)
    for name in DIRECT_CATS + (['cn'] if full else []):
        out.append('  %s:' % name)
        out.append('    type: http')
        out.append('    behavior: domain')
        out.append('    format: yaml')
        out.append('    url: "%s/%s.yaml"' % (CDN, name))
        out.append('    path: ./ruleset/%s.yaml' % name)
        out.append('    interval: 86400')
    out.append('')
    out.append('rules:')
    out.append('  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve')
    out.append('  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve')
    out.append('  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve')
    out.append('  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve')
    out.append('  - IP-CIDR6,fc00::/7,DIRECT,no-resolve')
    out.append('  - IP-CIDR6,fe80::/10,DIRECT,no-resolve')
    for name in DIRECT_CATS:
        out.append('  - RULE-SET,%s,DIRECT' % name)
    for name in PROXY_CATS:
        out.append('  - RULE-SET,%s,PROXY' % name)
    if full:
        out.append('  # cn 全量兜底，排在所有 PROXY 之后、GEOIP 之前')
        out.append('  - RULE-SET,cn,DIRECT')
    out.append('  - GEOIP,CN,DIRECT')
    out.append('  - MATCH,PROXY')
    out.append('')

    p = os.path.join(OUT, name_out)
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    return p


# --------------------------------------------------------------- providers 片段
def write_providers(data, today, full=False):
    name_out = 'providers-full.yaml' if full else 'providers.yaml'
    out = []
    out.append('# ============================================================')
    out.append('# bridge —— 可粘贴片段（不含任何节点）%s' % ('· 完整版（含 cn）' if full else ''))
    out.append('# 生成: %s   %s' % (today, SIGN))
    out.append('#')
    out.append('# 把 rule-providers 段合并进你现有配置的 rule-providers，')
    out.append('# 把 rules 段按顺序插到你的 rules 里（GEOIP,CN 之前）。')
    out.append('# 顺序不能变：fix-* 必须最前，cn 必须排在所有 PROXY 规则之后。')
    out.append('# ============================================================')
    out.append('')
    out.append('rule-providers:')
    for name in PROXY_CATS + DIRECT_CATS + (['cn'] if full else []):
        out.append('  %s:' % name)
        out.append('    type: http')
        out.append('    behavior: domain')
        out.append('    format: yaml')
        out.append('    url: "%s/%s.yaml"' % (CDN, name))
        out.append('    path: ./ruleset/bridge-%s.yaml' % name)
        out.append('    interval: 86400')
    out.append('')
    out.append('rules:')
    for name in DIRECT_CATS:
        out.append('  - RULE-SET,%s,DIRECT' % name)
    for name in PROXY_CATS:
        out.append('  - RULE-SET,%s,PROXY' % name)
    if full:
        out.append('  - RULE-SET,cn,DIRECT')
    out.append('')

    p = os.path.join(OUT, name_out)
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    return p


# --------------------------------------------------------------- sing-box
def write_singbox(domains, fname, today):
    """sing-box rule_set（source 格式）。JSON 不能带注释，说明写进 README。"""
    obj = {
        'version': 3,
        'rules': [
            {'domain_suffix': domains}
        ]
    }
    body = json.dumps(obj, ensure_ascii=False, indent=2)
    p = os.path.join(OUT, fname)
    io.open(p, 'w', encoding='utf-8', newline='\n').write(body + '\n')
    return p, len(domains)


# --------------------------------------------------------------- classical 规则集
def write_classical(domains, fname, label, today):
    """Surge / Shadowrocket / Clash classical / Stash 通用的 DOMAIN-SUFFIX 列表。
    规则行本身不带策略名——策略在使用处指定（RULE-SET,<url>,<policy>）。"""
    out = []
    out.append('# bridge —— %s' % label)
    out.append('# 生成: %s   %s' % (today, SIGN))
    out.append('# 格式: DOMAIN-SUFFIX，不含策略名，策略在使用处指定')
    out.append('# Clash 引用时 behavior 用 classical；Surge 用 RULE-SET')
    for d in domains:
        out.append('DOMAIN-SUFFIX,%s' % d)
    out.append('')
    p = os.path.join(OUT, fname)
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    return p


# --------------------------------------------------------------- 纯域名
def write_plain(domains, fname, label, today):
    out = []
    out.append('# bridge —— %s' % label)
    out.append('# 生成: %s   %s' % (today, SIGN))
    out.append('# 每行一个域名，匹配其全部子域。任何客户端都能吃。')
    out.extend(domains)
    out.append('')
    p = os.path.join(OUT, fname)
    io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    return p


def main():
    today = datetime.date.today().isoformat()
    if not os.path.isdir(OUT):
        os.makedirs(OUT)

    data = load_all()
    cn = parse_ruleset('cn')

    proxy = uniq([d for n in PROXY_CATS for d in data[n]])
    direct = uniq([d for n in DIRECT_CATS for d in data[n]])
    # 完整版的直连侧：修正层 + apple + cn 全量（cn 在后，重叠时前者优先）
    direct_full = uniq(direct + cn)

    p1 = write_clash(data, today)
    p2 = write_providers(data, today)
    p3, n3 = write_singbox(proxy, 'singbox-proxy.json', today)
    p4, n4 = write_singbox(direct, 'singbox-direct.json', today)
    p5 = write_classical(proxy, 'list.txt', '该走代理的域名（classical 规则集）', today)
    p5b = write_classical(direct, 'list-direct.txt', '该强制直连的域名（classical 规则集）', today)
    p6 = write_plain(proxy, 'domains.txt', '该走代理的域名清单', today)
    p7 = write_plain(direct, 'domains-direct.txt', '该强制直连的域名清单', today)

    pf1 = write_clash(data, today, full=True)
    pf2 = write_providers(data, today, full=True)
    pf3, nf3 = write_singbox(direct_full, 'singbox-direct-full.json', today)
    pf4 = write_classical(direct_full, 'list-direct-full.txt',
                          '该强制直连的域名（classical 规则集，含 cn 全量）', today)
    pf5 = write_plain(direct_full, 'domains-direct-full.txt',
                      '该强制直连的域名清单（含 cn 全量）', today)

    print('=' * 56)
    print('bridge export  %s' % today)
    print('=' * 56)
    print('  代理域名 %d 条 / 直连域名 %d 条 / 直连全量 %d 条'
          % (len(proxy), len(direct), len(direct_full)))
    for p, n in ((p1, ''), (p2, ''), (p3, n3), (p4, n4),
                 (p5, len(proxy)), (p5b, len(direct)),
                 (p6, len(proxy)), (p7, len(direct))):
        print('  %-28s %s' % (os.path.relpath(p, BASE), n))
    print('  --- 完整版（含 cn %d 条）---' % len(cn))
    for p, n in ((pf1, ''), (pf2, ''), (pf3, nf3),
                 (pf4, len(direct_full)), (pf5, len(direct_full))):
        print('  %-28s %s' % (os.path.relpath(p, BASE), n))
    print()
    return True


if __name__ == '__main__':
    main()
