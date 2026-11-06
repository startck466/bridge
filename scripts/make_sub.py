#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成含节点的 FlClash 配置（私有，不入公开仓库）

用法:
    python3 make_sub.py [--base=规则集URL前缀] [--out=路径]

默认规则源走 jsDelivr（国内可直连，24 小时缓存）:
    https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule

注意: cdn.jsdelivr.net 与 raw.githubusercontent.com 在国内均被 DNS 污染，
必须用 fastly / gcore / testingcf 这几个子域。
"""

import io
import os
import sys
import datetime

DEFAULT_OUT = 'D:/ai/ai-vps/.workbuddy/tmp/bridge-sub.yaml'
DEFAULT_BASE = 'https://fastly.jsdelivr.net/gh/startck466/bridge@main/rule'

def load_nodes():
    """从 scripts/.hosts.json 读节点 YAML（节点凭据绝不入库）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.hosts.json')
    if not os.path.isfile(path):
        sys.exit('缺少 %s，无法生成订阅（节点信息只存这里）' % path)
    import json as _json
    with io.open(path, encoding='utf-8') as f:
        nodes = _json.load(f).get('nodes_yaml')
    if not nodes or '- name:' not in nodes:
        sys.exit('.hosts.json 缺 nodes_yaml')
    return '\n' + nodes.lstrip('\n')


RULES_BLOCK = '''rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - IP-CIDR6,fc00::/7,DIRECT,no-resolve
  - IP-CIDR6,fe80::/10,DIRECT,no-resolve
  - RULE-SET,fix-direct,DIRECT
  - RULE-SET,fix-proxy,🚀 节点选择
  - RULE-SET,ai,🚀 节点选择
  - RULE-SET,dev,🚀 节点选择
  - RULE-SET,social,🚀 节点选择
  - RULE-SET,media,🚀 节点选择
  - RULE-SET,cloud,🚀 节点选择
  - RULE-SET,apple,DIRECT
  - RULE-SET,cn,DIRECT
  - GEOIP,CN,DIRECT
  - MATCH,🚀 节点选择
'''

GROUPS = '''proxy-groups:
  - name: "🚀 节点选择"
    type: select
    proxies:
      - "🇭🇰 香港双线"
      - "🇭🇰 ZGO-HK-HY2"
      - "🇭🇰 ZGO-HK"
      - "🇺🇸 US-IPv6"
      - DIRECT

  - name: "🇭🇰 香港双线"
    type: fallback
    url: "https://www.gstatic.com/generate_204"
    interval: 180
    timeout: 3000
    proxies:
      - "🇭🇰 ZGO-HK-HY2"
      - "🇭🇰 ZGO-HK"
'''

HEAD = '''# bridge 订阅配置（含节点，请勿公开）
# 生成时间: {today}
# 规则源: {base}
# 规则逻辑见 bridge 仓库 RULES.md

mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: true
unified-delay: true
tcp-concurrent: true
find-process-mode: off
global-client-fingerprint: chrome

dns:
  enable: true
  ipv6: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  fake-ip-filter:
    - "*.lan"
    - "*.local"
    - "localhost.ptlogin2.qq.com"
  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  proxy-server-nameserver:
    - https://dns.alidns.com/dns-query
    - https://doh.pub/dns-query
  nameserver:
    - https://dns.alidns.com/dns-query
    - https://doh.pub/dns-query
  nameserver-policy:
    # CN 域名跟随本机当前网络 DNS(system)：家里网关指向 AdGuard 即自动去广告；
    # 换网络自动跟随新网关，不会因写死内网 IP 而在外网失联
    "geosite:cn":
      - system
    "geosite:geolocation-!cn":
      - https://1.1.1.1/dns-query
      - https://8.8.8.8/dns-query
  fallback:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  fallback-filter:
    geoip: true
    geoip-code: CN

proxies:
'''

PROVIDERS = '''rule-providers:
  fix-direct:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/fix-direct.yaml"
    path: ./ruleset/fix-direct.yaml
    interval: 86400
  fix-proxy:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/fix-proxy.yaml"
    path: ./ruleset/fix-proxy.yaml
    interval: 86400
  ai:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/ai.yaml"
    path: ./ruleset/ai.yaml
    interval: 86400
  dev:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/dev.yaml"
    path: ./ruleset/dev.yaml
    interval: 86400
  social:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/social.yaml"
    path: ./ruleset/social.yaml
    interval: 86400
  media:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/media.yaml"
    path: ./ruleset/media.yaml
    interval: 86400
  cloud:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/cloud.yaml"
    path: ./ruleset/cloud.yaml
    interval: 86400
  apple:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/apple.yaml"
    path: ./ruleset/apple.yaml
    interval: 86400
  cn:
    type: http
    behavior: domain
    format: yaml
    url: "{base}/cn.yaml"
    path: ./ruleset/cn.yaml
    interval: 86400
'''


def build_config(base=DEFAULT_BASE):
    """返回完整订阅配置文本（含节点明文）。"""
    base = base.rstrip('/')
    today = datetime.date.today().isoformat()
    parts = [
        HEAD.format(today=today, base=base),
        load_nodes(),
        GROUPS,
        PROVIDERS.format(base=base),
        RULES_BLOCK,
    ]
    return '\n'.join(parts)


def main():
    base = DEFAULT_BASE
    out = DEFAULT_OUT
    for a in sys.argv[1:]:
        if a.startswith('--base='):
            base = a.split('=', 1)[1]
        elif a.startswith('--out='):
            out = a.split('=', 1)[1]
    base = base.rstrip('/')

    content = build_config(base)

    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    try:
        import yaml
        cfg = yaml.safe_load(content)
        print('OK  %s' % out)
        print('    proxies=%d  groups=%d  providers=%d  rules=%d' % (
            len(cfg['proxies']), len(cfg['proxy-groups']),
            len(cfg['rule-providers']), len(cfg['rules'])))
    except ImportError:
        print('OK  %s（未装 pyyaml，跳过校验）' % out)


if __name__ == '__main__':
    main()
