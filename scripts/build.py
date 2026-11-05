#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge 规则库构建脚本

用法:
    python3 build.py                # 构建全部规则集 + 订阅配置
    python3 build.py --stats        # 只统计，不写文件

输入:
    scripts/src/*.txt   手工分类清单（人工维护，准确性来源）
    scripts/data/direct.txt  国内全量域名（公共库清洗后）

输出:
    rule/*.yaml         mihomo rule-provider 格式
    dist/flclash.yaml   FlClash 订阅配置（节点部分为占位符）
"""

import io
import os
import re
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'scripts', 'src')
DATA = os.path.join(BASE, 'scripts', 'data')
RULE = os.path.join(BASE, 'rule')
DIST = os.path.join(BASE, 'dist')

# 分类 -> 说明。顺序即规则匹配优先级（不含 cn，cn 在最后兜底）
CATS = [
    ('fix-direct', '修正层：该直连却被误判为代理'),
    ('fix-proxy',  '修正层：该代理却被误判为直连'),
    ('ai',         'AI 服务'),
    ('dev',        '开发者服务'),
    ('social',     '国外社交 / 即时通讯'),
    ('media',      '国外流媒体 / 影音'),
    ('cloud',      '国外云服务 / 基础设施'),
    ('apple',      '苹果服务'),
    ('cn',         '国内域名（全量兜底）'),
]

DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9\-_]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-_]*[a-z0-9])?)+$')
IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')


def load_txt(path):
    """读源文件，去注释/空行，规范化"""
    if not os.path.exists(path):
        return []
    out = []
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 允许 "domain.com  # 注释" 形式
            line = line.split('#')[0].strip()
            if not line:
                continue
            out.append(line)
    return out


def normalize(domains):
    """小写、去前缀符号、过滤非法、去重"""
    seen = set()
    out = []
    for d in domains:
        d = d.strip().lower().strip('.')
        for p in ('+.', '*.', '.', 'full:', 'domain:', 'suffix:'):
            if d.startswith(p):
                d = d[len(p):]
        d = d.split('/')[0].split(':')[0]
        if not d or IP_RE.match(d) or not DOMAIN_RE.match(d):
            continue
        if len(d) > 253:
            continue
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def collapse_suffix(domains):
    """后缀归并：若父域已在集合中，子域是冗余的，删掉
    例：有 example.com 时，删掉 a.example.com
    纯根域清单收益有限，但对长尾清单能显著瘦身
    """
    s = set(domains)
    kept = []
    for d in domains:
        parts = d.split('.')
        redundant = False
        # 检查所有可能的父域（至少保留两段，如 example.com）
        for i in range(1, len(parts) - 1):
            if '.'.join(parts[i:]) in s:
                redundant = True
                break
        if not redundant:
            kept.append(d)
    return kept


def write_ruleset(name, domains, desc, today):
    path = os.path.join(RULE, name + '.yaml')
    lines = [
        '# bridge rule set: %s' % name,
        '# %s' % desc,
        '# count: %d | generated: %s' % (len(domains), today),
        '# DO NOT EDIT — 修改请改 scripts/src/%s.txt 后重新构建' % name,
        'payload:',
    ]
    for d in domains:
        lines.append("  - '+.%s'" % d)
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    return path, len(domains)


def build_subscription(cats_count, today, user='YOUR_GITHUB_USER', repo='bridge'):
    """生成 FlClash 订阅配置骨架。节点部分故意留空，由私有配置注入"""
    base = 'https://raw.githubusercontent.com/%s/%s/main/rule' % (user, repo)

    providers = []
    order = [c for c, _ in CATS]
    for name in order:
        target = 'DIRECT' if name in ('fix-direct', 'apple', 'cn') else 'PROXY'
        providers.append((name, target))

    out = []
    out.append('# bridge 订阅配置 —— 由 build.py 生成，请勿直接编辑')
    out.append('# 生成时间: %s' % today)
    out.append('# 规则说明见 https://github.com/%s/%s/blob/main/RULES.md' % (user, repo))
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
    out.append('    - "localhost.ptlogin2.qq.com"')
    out.append('  default-nameserver:')
    out.append('    - 223.5.5.5')
    out.append('    - 119.29.29.29')
    out.append('  nameserver:')
    out.append('    - https://dns.alidns.com/dns-query')
    out.append('    - https://doh.pub/dns-query')
    out.append('')
    out.append('proxies: []   # 节点由私有配置注入，规则库本身不含节点')
    out.append('')
    out.append('proxy-groups:')
    out.append('  - name: "PROXY"')
    out.append('    type: select')
    out.append('    proxies:')
    out.append('      - "节点选择"')
    out.append('      - DIRECT')
    out.append('')
    out.append('rule-providers:')
    for name, _ in providers:
        out.append('  %s:' % name)
        out.append('    type: http')
        out.append('    behavior: domain')
        out.append('    format: yaml')
        out.append('    url: "%s/%s.yaml"' % (base, name))
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
    for name, target in providers:
        out.append('  - RULE-SET,%s,%s' % (name, target))
    out.append('  - GEOIP,CN,DIRECT')
    out.append('  - MATCH,PROXY')
    out.append('')

    path = os.path.join(DIST, 'flclash.yaml')
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out))
    return path


def main():
    today = datetime.date.today().isoformat()
    stats_only = '--stats' in sys.argv
    user = 'YOUR_GITHUB_USER'
    for a in sys.argv:
        if a.startswith('--user='):
            user = a.split('=', 1)[1]

    for d in (SRC, RULE, DIST):
        if not os.path.isdir(d):
            os.makedirs(d)

    print('=' * 56)
    print('bridge build  %s' % today)
    print('=' * 56)

    total = 0
    counts = {}
    for name, desc in CATS:
        if name == 'cn':
            raw = load_txt(os.path.join(DATA, 'direct.txt'))
            source = 'data/direct.txt'
        else:
            raw = load_txt(os.path.join(SRC, name + '.txt'))
            source = 'src/%s.txt' % name

        cleaned = normalize(raw)
        # 大清单做后缀归并，小清单不做（保持可读性）
        if name == 'cn':
            before = len(cleaned)
            cleaned = collapse_suffix(cleaned)
            collapsed = before - len(cleaned)
        else:
            collapsed = 0

        if not cleaned:
            print('  %-12s  跳过（无数据）' % name)
            continue

        counts[name] = len(cleaned)
        total += len(cleaned)
        if not stats_only:
            write_ruleset(name, cleaned, desc, today)
        note = '  归并 -%d' % collapsed if collapsed else ''
        print('  %-12s %7d 条   <- %s%s' % (name, len(cleaned), source, note))

    print('-' * 56)
    print('  合计 %d 条（去重后）' % total)

    if not stats_only:
        p = build_subscription(counts, today, user)
        print('  订阅配置: %s' % os.path.relpath(p, BASE))
    print()


if __name__ == '__main__':
    main()
