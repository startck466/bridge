#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge 每周维护

流程:
  1. 拉取上游国内域名清单（走 jsDelivr，不碰香港服务器）
  2. 重建全部规则集
  3. 推送 GitHub（startck466/bridge）
  4. 刷新 jsDelivr 边缘缓存并逐项校验

客户端（FlClash / mihomo）通过 jsDelivr 拉规则集，24 小时自动刷新一次。

服务器不再参与任何规则分发，仅作为拉取上游失败时的兜底通道。

用法:
    python3 scripts/weekly.py            # 完整流程
    python3 scripts/weekly.py --no-push  # 只本地重建，不推 GitHub

GitHub 凭据从 scripts/.hosts.json 读取（已 gitignore，不入库）
"""

import io
import json
import os
import subprocess
import sys
import datetime
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(BASE, 'scripts', '.hosts.json')
DATA = os.path.join(BASE, 'scripts', 'data')
RULE = os.path.join(BASE, 'rule')

RULES = ['fix-direct', 'fix-proxy', 'ai', 'dev', 'social', 'media',
         'cloud', 'apple', 'cn']

# 上游清单的多个通道，按顺序试
UPSTREAM_MIRRORS = [
    'https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt',
    'https://gcore.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt',
    'https://testingcf.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt',
    'https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/direct-list.txt',
    'https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/direct-list.txt',
]


def load_conf():
    if not os.path.isfile(CONF):
        sys.exit('缺少 %s（含 GitHub 凭据）' % CONF)
    return json.load(io.open(CONF, encoding='utf-8'))


def http_get(url, timeout=90):
    req = urllib.request.Request(url, headers={'User-Agent': 'bridge-weekly/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------------------------------------------------------- 1. 拉上游
def step_fetch():
    """下载上游国内域名清单。任一通道成功即止。"""
    local = os.path.join(DATA, 'direct.txt')
    if not os.path.isdir(DATA):
        os.makedirs(DATA)

    body = None
    for url in UPSTREAM_MIRRORS:
        host = url.split('/')[2]
        try:
            st, body = http_get(url)
            if st == 200 and len(body) > 500000:
                print('  通道 %-24s OK  %.1f MB' % (host, len(body) / 1048576.0))
                break
            print('  通道 %-24s HTTP=%s 体积异常' % (host, st))
            body = None
        except Exception as e:
            print('  通道 %-24s 失败 %s' % (host, str(e)[:60]))
            body = None
    if body is None:
        if os.path.isfile(local):
            print('  !! 全部通道失败，沿用现有 direct.txt')
            return
        sys.exit('!! 上游不可达且本地无数据，中止')

    tmp = local + '.new'
    with io.open(tmp, 'wb') as f:
        f.write(body)
    n = sum(1 for _ in io.open(tmp, encoding='utf-8', errors='replace'))
    old = 0
    if os.path.isfile(local):
        old = sum(1 for _ in io.open(local, encoding='utf-8', errors='replace'))

    # 行数骤降 20% 以上视为上游异常，保留旧数据
    if old and n < old * 0.8:
        print('  !! 上游 %d 行 → %d 行，降幅过大，保留旧数据' % (old, n))
        os.remove(tmp)
    else:
        if os.path.exists(local):
            os.remove(local)
        os.rename(tmp, local)
        print('  direct.txt  %d 行（旧 %d，差 %+d）' % (n, old, n - old))


# ---------------------------------------------------------------- 2. 构建
def step_build():
    r = subprocess.run([sys.executable, os.path.join(BASE, 'scripts', 'build.py')],
                       cwd=BASE, capture_output=True)
    txt = r.stdout.decode('utf-8', 'replace')
    print('\n'.join('  ' + l for l in txt.strip().splitlines()[-12:]))
    if r.returncode != 0:
        sys.exit('构建失败：' + r.stderr.decode('utf-8', 'replace')[:400])


def step_export():
    """导出给其它客户端的规则订阅（纯域名，无节点）。"""
    r = subprocess.run([sys.executable, os.path.join(BASE, 'scripts', 'export.py')],
                       cwd=BASE, capture_output=True)
    txt = r.stdout.decode('utf-8', 'replace')
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    print('\n'.join('  ' + l for l in lines[-10:]))
    if r.returncode != 0:
        print('  !! 导出失败（不影响规则集，仅 export/ 未更新）：%s'
              % r.stderr.decode('utf-8', 'replace')[:200])
        return False
    return True


# ---------------------------------------------------------------- 3. 推送
def step_push(gh):
    repo = gh['repo']
    subprocess.run(['git', 'add', '-A'], cwd=BASE, capture_output=True)

    diff = subprocess.run(['git', 'status', '--porcelain'], cwd=BASE,
                          capture_output=True).stdout.decode('utf-8', 'replace')
    if not diff.strip():
        print('  无变动，跳过提交')
        return False

    msg = 'weekly: 规则更新 %s' % datetime.date.today().isoformat()
    r = subprocess.run(['git', '-c', 'core.autocrlf=false', 'commit', '-m', msg],
                       cwd=BASE, capture_output=True)
    if r.returncode != 0:
        print('  提交失败：', r.stderr.decode('utf-8', 'replace')[:200])
        return False

    tok = gh.get('token')
    url = 'https://%s:%s@github.com/%s.git' % (gh['user'], tok, repo)
    r = subprocess.run(['git', 'push', url, 'HEAD:main'],
                       cwd=BASE, capture_output=True, timeout=300)
    if r.returncode != 0:
        err = r.stderr.decode('utf-8', 'replace').replace(tok, '***')
        print('  推送失败：', err.strip()[-300:])
        return False
    print('  已推送 %s' % repo)
    return True


# ---------------------------------------------------------------- 4. 刷新 + 校验
def step_verify(gh):
    base = gh['rule_base']
    print('  jsDelivr 缓存刷新:')
    for name in RULES:
        purge = 'https://purge.jsdelivr.net/gh/%s@main/rule/%s.yaml' % (gh['repo'], name)
        try:
            http_get(purge, timeout=45)
        except Exception:
            pass
    print('  已完成')

    print('  逐项校验:')
    ok = bad = 0
    for name in RULES:
        url = '%s/%s.yaml' % (base, name)
        try:
            st, body = http_get(url)
            head = body[:200].decode('utf-8', 'replace')
            # 内容必须是我们生成的，不能是 CDN 错误页
            good = (st == 200 and head.startswith('# bridge rule set'))
            n = head.split('count: ')[1].split(' ')[0] if 'count: ' in head else '?'
            print('    %-12s HTTP=%s  %7s B  count=%s  %s'
                  % (name, st, len(body), n, 'OK' if good else '<<< 内容异常'))
            ok += 1 if good else 0
            bad += 0 if good else 1
        except Exception as e:
            print('    %-12s 失败 %s' % (name, str(e)[:60]))
            bad += 1
    print('  校验: 成功 %d / 失败 %d' % (ok, bad))
    return bad == 0


def step_seal():
    """重新加密订阅（节点或配置结构变化时才有实质改动）。"""
    r = subprocess.run([sys.executable, os.path.join(BASE, 'scripts', 'seal_sub.py')],
                       cwd=BASE, capture_output=True)
    txt = (r.stdout or b'').decode('utf-8', 'replace') + (r.stderr or b'').decode('utf-8', 'replace')
    print('\n'.join('  ' + l for l in txt.strip().splitlines()[:6]))
    if r.returncode != 0:
        print('  !! 订阅加密失败，本次沿用旧密文（不影响规则集）')
        return False
    return True


def main():
    do_push = '--no-push' not in sys.argv
    conf = load_conf()
    gh = conf['github']

    print('=' * 56)
    print('bridge weekly  %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
    print('=' * 56)

    print('\n[1/5] 拉取上游国内域名清单')
    step_fetch()

    print('\n[2/6] 重建规则集')
    step_build()

    print('\n[3/6] 导出通用规则订阅（不含节点）')
    step_export()

    print('\n[4/6] 加密订阅（AES-256-GCM）')
    step_seal()

    pushed = False
    if do_push:
        print('\n[5/6] 推送 GitHub')
        pushed = step_push(gh)
    else:
        print('\n[5/6] 跳过推送（--no-push）')

    print('\n[6/6] 刷新缓存并校验')
    ok = step_verify(gh)

    print('\n结果: %s' % ('全部正常' if ok else '存在失败，需人工检查'))
    if not pushed and do_push:
        print('注意: 本次没有新提交，线上规则未变化')


if __name__ == '__main__':
    main()
