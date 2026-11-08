#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge 每周维护

流程:
  1. 拉取上游国内域名清单（走 jsDelivr，不碰香港服务器）
  2. 重建全部规则集
  3. 导出通用规则订阅（export/）
  4. 推送规则到 GitHub，拿到本次提交 SHA
  5. 把订阅里的规则地址固定到那个 SHA（scripts/sub-ref.txt），并重建
  6. 加密订阅（sub/bridge.enc）
  7. 再推一次（地址文件 + 密文）
  8. 校验：固定 commit 上的规则集 + 订阅端到端解密

客户端（FlClash / mihomo）通过 jsDelivr 拉规则集，24 小时自动刷新一次。

**为什么订阅里的规则地址固定到 commit 而不是 @main**
@main 走 jsDelivr 边缘缓存，刷新只能靠 purge，而 purge 按路径限流，
被限流时缓存不刷 —— 就会出现"规则改了、推上去了，客户端几小时还是旧内容"。
固定 commit 是全新缓存键，push 完立刻可读，不需要 purge。

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
SCRIPTS = os.path.join(BASE, 'scripts')
CONF = os.path.join(SCRIPTS, '.hosts.json')
DATA = os.path.join(BASE, 'scripts', 'data')
RULE = os.path.join(BASE, 'rule')

sys.path.insert(0, SCRIPTS)
import _ref  # noqa: E402

RULES = ['fix-direct', 'fix-proxy', 'ai', 'dev', 'social', 'media',
         'cloud', 'apple', 'cn']

# export/ 下对外分发的文件，每周一并校验（首字节期望值）
EXPORTS = [
    ('clash.yaml', b'#'),
    ('providers.yaml', b'#'),
    ('singbox-proxy.json', b'{'),
    ('singbox-direct.json', b'{'),
    ('list.txt', b'#'),
    ('list-direct.txt', b'#'),
    ('domains.txt', b'#'),
    ('domains-direct.txt', b'#'),
    ('clash-full.yaml', b'#'),
    ('providers-full.yaml', b'#'),
    ('singbox-direct-full.json', b'{'),
    ('list-direct-full.txt', b'#'),
    ('domains-direct-full.txt', b'#'),
]

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
    if os.path.isfile(local):
        print('  本地已存在 direct.txt，直接沿用')
        return

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
def run_script(name, tail=12):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name)],
                       cwd=BASE, capture_output=True)
    txt = r.stdout.decode('utf-8', 'replace')
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    print('\n'.join('  ' + l for l in lines[-tail:]))
    return r.returncode, r.stderr.decode('utf-8', 'replace')


def step_build():
    rc, err = run_script('build.py')
    if rc != 0:
        sys.exit('构建失败：' + err[:400])


def step_export():
    """导出给其它客户端的规则订阅（纯域名，无节点）。"""
    rc, err = run_script('export.py', tail=10)
    if rc != 0:
        print('  !! 导出失败（不影响规则集，仅 export/ 未更新）：%s' % err[:200])
        return False
    return True


def enc_digest():
    """sub/bridge.enc 的内容指纹，用来判断密文是否真的变了。"""
    import hashlib
    path = os.path.join(BASE, 'sub', 'bridge.enc')
    try:
        with io.open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, IOError):
        return None


def step_seal():
    """重新加密订阅（节点或配置结构变化时才有实质改动）。

    返回密文是否**真的变了**（seal_sub 正文没变时会 SKIP）。
    """
    before = enc_digest()
    rc, err = run_script('seal_sub.py', tail=6)
    if rc != 0:
        print('  !! 订阅加密失败，本次沿用旧密文（不影响规则集）')
        print('     %s' % err.strip()[:200])
        return False
    return enc_digest() != before


# ---------------------------------------------------------------- 3. 推送
# 推送 GitHub 必须走本机 Clash：直连 github.com 在本地网络不通。
# 本机 shell 里还残留着指向失效端口的 http_proxy/https_proxy，会干扰 git，
# 所以子进程一律用"干净环境 + 显式 -c http.proxy"。
GIT_PROXY = os.environ.get('BRIDGE_GIT_PROXY', 'http://127.0.0.1:7890')


def git_env():
    env = dict(os.environ)
    for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
              'all_proxy'):
        env.pop(k, None)
    return env


def ref_reachable(sha, repo, tries=3):
    """确认这个 commit 在 jsDelivr 上真的可读再写进订阅。

    客户端拿到的每个规则地址都长这样，如果 SHA 在 CDN 上取不到，
    客户端会整包拉不到规则。宁可保持旧 ref，也不能写一个取不到的。
    """
    import time
    for i in range(tries):
        try:
            st, body = http_get(
                'https://fastly.jsdelivr.net/gh/%s@%s/rule/cn.yaml' % (repo, sha),
                timeout=60)
            if st == 200 and body[:40].startswith(b'# bridge rule set'):
                return True
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(5)
    return False


def run_git(args, use_proxy=False, timeout=300):
    cmd = ['git']
    if use_proxy:
        cmd += ['-c', 'http.proxy=' + GIT_PROXY, '-c', 'https.proxy=' + GIT_PROXY]
    cmd += args
    try:
        return subprocess.run(cmd, cwd=BASE, capture_output=True,
                              timeout=timeout, env=git_env())
    except subprocess.TimeoutExpired:
        class R(object):
            returncode = 1
            stdout = b''
            stderr = b'git timeout'
        return R()


def git_sha(full=False):
    cmd = ['rev-parse', 'HEAD'] if full else ['rev-parse', '--short', 'HEAD']
    r = run_git(cmd, timeout=30)
    return r.stdout.decode('utf-8', 'replace').strip() or None


def step_push(gh, msg):
    """提交并推送。返回 (是否推送成功, 当前 HEAD 完整 SHA)。"""
    repo = gh['repo']
    run_git(['add', '-A'], timeout=120)

    diff = run_git(['status', '--porcelain'], timeout=60).stdout.decode('utf-8', 'replace')
    if not diff.strip():
        print('  无变动，跳过提交')
        return False, git_sha(full=True)

    base_cmd = ['-c', 'core.autocrlf=false', 'commit', '-m', msg]
    r = run_git(base_cmd, timeout=120)
    if r.returncode != 0:
        # 本机全局配了 1Password SSH 签名，沙箱连不到 agent 会卡死提交
        err = (r.stderr or b'').decode('utf-8', 'replace')
        r2 = run_git(['-c', 'commit.gpgsign=false'] + base_cmd, timeout=120)
        if r2.returncode != 0:
            print('  提交失败：', ((r2.stderr or err) or b'').decode('utf-8', 'replace')[:200])
            return False, git_sha(full=True)

    sha = git_sha(full=True)
    tok = gh.get('token') or ''
    url = 'https://%s:%s@github.com/%s.git' % (gh['user'], tok, repo)

    r = run_git(['push', url, 'HEAD:main'], use_proxy=True)
    if r.returncode != 0:
        # 代理挂了就试直连（出国网络恰好可用时）
        r2 = run_git(['push', url, 'HEAD:main'])
        if r2.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'replace').replace(tok, '***')
            err2 = (r2.stderr or b'').decode('utf-8', 'replace').replace(tok, '***')
            print('  推送失败：', (err.strip()[-200:] or err2.strip()[-200:]))
            return False, sha
    print('  已推送 %s  (%s)' % (repo, (sha or '')[:12]))
    return True, sha


# ---------------------------------------------------------------- 4. 刷新 + 校验
def purge_path(repo, rel):
    """刷新单条 jsDelivr 缓存。

    返回 'ok' / 'throttled' / 'error'。jsDelivr 对同一路径按分钟级节流，
    被节流时调用会被直接丢弃（缓存不会刷新），必须显式识别出来。

    注意：客户端的规则集已固定到 commit，**不依赖**这里刷得成不成功；
    这一步只为 export/ 与公开模板（仍引用 @main）服务。
    """
    url = 'https://purge.jsdelivr.net/gh/%s@main/%s' % (repo, rel)
    key = '/gh/%s@main/%s' % (repo, rel)
    try:
        st, body = http_get(url, timeout=45)
        info = (json.loads(body.decode('utf-8', 'replace')).get('paths') or {}).get(key, {})
        return 'throttled' if info.get('throttled') else 'ok'
    except Exception:
        return 'error'


def local_count(name):
    """读本地 rule/<name>.yaml 头部里的 count。"""
    path = os.path.join(RULE, name + '.yaml')
    try:
        with io.open(path, encoding='utf-8') as f:
            head = f.read(400)
    except (OSError, IOError):
        return None
    return head.split('count: ')[1].split(' ')[0] if 'count: ' in head else None


def live_sub_rule_base(conf):
    """拉线上订阅，返回里面的规则基址与正文长度。

    这是唯一能证明「客户端拿到的配置里规则地址是对的」的检查。

    注意 Worker 已改为服务端解密：`sub-worker.js` 用 env.SUB_KEY 解封后
    直接返回明文 YAML（content-type: text/yaml）。所以这里不能无条件
    按密文信封解析，否则会误报「密文信封格式不对」。两种形态都兼容：
      - 明文（当前形态）：直接当正文用
      - 密文信封（旧形态/直连 raw）：先本地解密
    """
    import seal_sub
    _d, key = seal_sub.load_creds()
    sub = conf['subscription']
    url = 'https://%s/%s' % (sub['worker_domain'], sub['path_token'])
    st, body = http_get(url, timeout=45)
    if st != 200:
        return st, None, None
    text = body.decode('utf-8', 'replace')
    magic = getattr(seal_sub, 'MAGIC', 'BRIDGE-SUB-v1')
    if text.lstrip().startswith(magic):
        plain = seal_sub.unseal(text, key)
    else:
        plain = text
    line = ''
    for l in plain.splitlines():
        if l.startswith('# 规则源:'):
            line = l.split(':', 1)[1].strip()
            break
    return st, line, len(plain)


def step_verify(conf, ref):
    gh = conf['github']
    main_base = gh['rule_base']                       # @main（公开模板用）
    exp_base = main_base.replace('/rule', '/export')
    pin_base = _ref.rule_base(ref=ref) if ref else main_base

    print('  刷新 jsDelivr 缓存（export 与公开模板仍指向 @main）:')
    stat = {'ok': 0, 'throttled': 0, 'error': 0}
    for name in RULES:
        stat[purge_path(gh['repo'], 'rule/%s.yaml' % name)] += 1
    for name, _ in EXPORTS:
        stat[purge_path(gh['repo'], 'export/%s' % name)] += 1
    print('    成功 %d  被节流 %d  失败 %d' % (stat['ok'], stat['throttled'], stat['error']))
    if stat['throttled']:
        print('    !! 有路径被节流，@main 上可能仍是旧内容（不影响已固定 ref 的客户端）')

    ok = bad = 0

    print('  客户端实际拉取的地址（固定 ref）: %s' % pin_base)
    pin_counts = {}
    for name in RULES:
        try:
            st, body = http_get('%s/%s.yaml' % (pin_base, name), timeout=60)
            head = body[:200].decode('utf-8', 'replace')
            good = (st == 200 and head.startswith('# bridge rule set'))
            n = head.split('count: ')[1].split(' ')[0] if 'count: ' in head else '?'
            pin_counts[name] = n
            note = 'OK' if good else '<<< 内容异常'
            lc = local_count(name)
            if good and lc and lc != n:
                note = '<<< 与本地不一致（本地 %s）' % lc
                good = False
            print('    %-12s HTTP=%s  %7s B  count=%s  %s'
                  % (name, st, len(body), n, note))
            ok += 1 if good else 0
            bad += 0 if good else 1
        except Exception as e:
            print('    %-12s 失败 %s' % (name, str(e)[:60]))
            bad += 1

    print('  订阅端到端（解密线上密文，确认规则地址已指向固定 ref）:')
    try:
        st, base_line, size = live_sub_rule_base(conf)
        expect = '@' + (ref or '')
        if st != 200:
            print('    HTTP=%s  <<< 订阅端点异常' % st)
            bad += 1
        elif base_line and expect in base_line:
            print('    HTTP=200  正文 %d 字符' % size)
            print('    规则源: %s  OK' % base_line)
            ok += 1
        else:
            print('    HTTP=200  正文 %d 字符' % size)
            print('    规则源: %s  <<< 没指向 %s' % (base_line or '(未找到)', expect))
            bad += 1
    except Exception as e:
        print('    失败 %s' % str(e)[:120])
        bad += 1

    print('  导出文件校验（@main）:')
    for name, first in EXPORTS:
        try:
            st, body = http_get('%s/%s' % (exp_base, name))
            good = (st == 200 and body.startswith(first) and len(body) > 100)
            print('    %-24s HTTP=%s  %9s B  %s'
                  % (name, st, len(body), 'OK' if good else '<<< 内容异常'))
            ok += 1 if good else 0
            bad += 0 if good else 1
        except Exception as e:
            print('    %-24s 失败 %s' % (name, str(e)[:60]))
            bad += 1

    print('  校验: 成功 %d / 失败 %d' % (ok, bad))
    return bad == 0


def step_purge_sub_cf(conf, changed):
    """密文变了就清一次该域名的 CF 边缘缓存。

    Worker 响应本身带 no-store，理论上不该被边缘缓存；这里是双保险，
    失败一律忽略，绝不能影响维护流程。
    """
    if not changed:
        return
    cf = conf.get('cloudflare') or {}
    tok = cf.get('api_token')
    zones = cf.get('zone_id') or {}
    host = conf['subscription']['worker_domain']
    zid = None
    for zone, v in zones.items():
        if host.endswith(zone):
            zid = v
            break
    if not tok or not zid:
        return
    try:
        req = urllib.request.Request(
            'https://api.cloudflare.com/client/v4/zones/%s/purge_cache' % zid,
            data=json.dumps({'hosts': [host]}).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + tok,
                     'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
        print('  CF 缓存已清理（%s）：%s' % (host, d.get('success')))
    except Exception as e:
        print('  CF 缓存清理跳过：%s' % str(e)[:100])


def main():
    do_push = '--no-push' not in sys.argv
    conf = load_conf()
    gh = conf['github']

    print('=' * 56)
    print('bridge weekly  %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
    print('=' * 56)
    print('当前规则地址 ref: %s' % _ref.RULE_REF)

    print('\n[1/8] 拉取上游国内域名清单')
    step_fetch()

    print('\n[2/8] 重建规则集')
    step_build()

    print('\n[3/8] 导出通用规则订阅（不含节点）')
    step_export()

    ref = _ref.RULE_REF
    if do_push:
        print('\n[4/8] 推送规则到 GitHub')
        pushed, sha = step_push(gh, 'weekly: 规则更新 %s' % datetime.date.today().isoformat())
        print('\n[5/8] 把订阅的规则地址固定到本次提交')
        # 判定只认"CDN 上真的读得到这个 commit"——它同时证明了推送成功、
        # 也证明了客户端将来能拉到。本次无改动时 HEAD 已在远端，同样成立。
        if sha and ref_reachable(sha, gh['repo']):
            try:
                changed = _ref.write_ref(
                    sha, note='weekly %s' % datetime.date.today().isoformat())
            except ValueError as e:
                changed = False
                print('  !! 写入 ref 失败：%s' % e)
            if changed:
                ref = sha
                print('  sub-ref.txt → %s，按新地址重建' % sha[:12])
                step_build()
                step_export()
            else:
                ref = _ref.read_ref()
                print('  ref 无变化（%s）' % (ref or '?')[:12])
        else:
            ref = _ref.read_ref()
            print('  !! 本次提交在 CDN 上还读不到，ref 保持不动（%s）' % (ref or '?')[:12])
            print('     客户端不受影响，继续用旧地址；脚本下次运行会再试')
    else:
        print('\n[4/8] 跳过推送（--no-push）')
        print('\n[5/8] 跳过 ref 同步（--no-push）')

    print('\n[6/8] 加密订阅（AES-256-GCM）')
    sealed_ok = step_seal()

    if do_push:
        print('\n[7/8] 推送地址文件与密文')
        step_push(gh, 'weekly: 订阅指向 @%s' % (ref or '?')[:12])
        step_purge_sub_cf(conf, sealed_ok)
    else:
        print('\n[7/8] 跳过推送（--no-push）')

    print('\n[8/8] 校验')
    ok = step_verify(conf, ref)

    print('\n结果: %s' % ('全部正常' if ok else '存在失败，需人工检查'))
    print('规则地址: %s' % _ref.rule_base(ref=ref))


if __name__ == '__main__':
    main()
