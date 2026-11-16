#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge 每周维护（公开仓库：只承载规则，不含节点、不含订阅令牌）

流程:
  1. 拉取上游国内域名清单（走 jsDelivr，不碰香港服务器）
  2. 重建全部规则集
  3. 导出通用规则订阅（export/，纯域名无节点）
  4. 推送规则到 GitHub，拿到本次提交 SHA
  5. 把规则地址固定到那个 SHA（scripts/rule-ref.txt），并重建
  6. 再推一次（地址文件）+ 刷新缓存并逐条校验

客户端（FlClash / Stash / mihomo）通过 jsDelivr 拉规则集，24 小时自动刷新一次。

**为什么规则地址固定到 commit 而不是 @main**
@main 走 jsDelivr 边缘缓存，刷新只能靠 purge，而 purge 按路径限流，
被限流时缓存不刷 —— 就会出现"规则改了、推上去了，客户端几小时还是旧内容"。
固定 commit 是全新缓存键，push 完立刻可读，不需要 purge。

**节点与私有订阅完全不在这里**
本仓库公开，只放规则。带节点的私有订阅由本机 `../bridge-private/` 下的
私有脚本生成并下发到自有服务器，凭据只存在 `scripts/.hosts.json`（已 gitignore）。
本脚本不引用、不调用任何私有发布逻辑，仓库里也没有订阅地址、令牌或节点。

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
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, 'scripts')
CONF = os.path.join(SCRIPTS, '.hosts.json')
DATA = os.path.join(BASE, 'scripts', 'data')
RULE = os.path.join(BASE, 'rule')

sys.path.insert(0, SCRIPTS)
import _ref  # noqa: E402

RULES = ['fix-direct', 'fix-proxy', 'ai', 'dev', 'social', 'media',
         'cloud', 'apple', 'cn']

# 父进程（charon_weekly）等待本脚本 600 秒。给收尾留余量，
# 所有网络校验都必须在这个预算内完成；超时只降级为"未完成"，不能把父进程拖死。
DEFAULT_BUDGET = 480
DEADLINE = 0.0


def set_budget(seconds):
    global DEADLINE
    seconds = int(seconds) if seconds else 0
    DEADLINE = time.monotonic() + seconds if seconds > 0 else 0.0


def _remaining():
    return None if not DEADLINE else DEADLINE - time.monotonic()


def _cap(timeout):
    rem = _remaining()
    return float(timeout) if rem is None else max(1.0, min(float(timeout), rem))


def _parallel_map(items, fn, workers=8):
    """保序并发执行；单项异常留在结果里，由调用方计为失败。"""
    results = [None] * len(items)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for fut in as_completed(futures):
            index = futures[fut]
            try:
                results[index] = fut.result()
            except Exception as exc:  # noqa: BLE001 - 单项失败不能中断其余项
                results[index] = exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


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
    # 优先用环境变量传入的 JSON（凭据保险库直供，本地不落明文）
    raw = os.environ.get('BRIDGE_HOSTS_JSON')
    if raw:
        return json.loads(raw)
    if not os.path.isfile(CONF):
        sys.exit('缺少 %s（含 GitHub 凭据）' % CONF)
    return json.load(io.open(CONF, encoding='utf-8'))


def http_get(url, timeout=90):
    rem = _remaining()
    if rem is not None and rem <= 1:
        raise TimeoutError('weekly_budget_exhausted')
    req = urllib.request.Request(url, headers={'User-Agent': 'bridge-weekly/1.0'})
    with urllib.request.urlopen(req, timeout=_cap(timeout)) as r:
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

    body = None
    for url in UPSTREAM_MIRRORS:
        remaining = _remaining()
        if remaining is not None and remaining < 30:
            print('  剩余预算不足，停止尝试上游通道')
            break
        try:
            st, body = http_get(url, timeout=45)
            if st == 200 and body:
                print('  上游通道: %s' % url.split('/')[2])
                break
        except Exception as e:
            print('  通道失败 %s: %s' % (url.split('/')[2], str(e)[:60]))
            body = None
    if not body:
        sys.exit('  所有上游通道均失败')

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
def run_script(name, tail=12, timeout=180):
    try:
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name)],
                           cwd=BASE, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print('  %s 超时（%ss），终止本轮构建' % (name, timeout))
        return 1, 'timeout'
    txt = r.stdout.decode('utf-8', 'replace')
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    print('\n'.join('  ' + l for l in lines[-tail:]))
    return r.returncode, r.stderr.decode('utf-8', 'replace')


def step_build():
    rc, err = run_script('build.py', timeout=240)
    if rc != 0:
        sys.exit('构建失败：' + err[:400])


def step_export():
    """导出给其它客户端的规则订阅（纯域名，无节点）。"""
    rc, err = run_script('export.py', tail=10, timeout=120)
    if rc != 0:
        print('  !! 导出失败（不影响规则集，仅 export/ 未更新）：%s' % err[:200])
        return False
    return True


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


def ref_reachable(sha, repo, tries=2):
    """确认这个 commit 在 jsDelivr 上真的可读再写进订阅。

    客户端拿到的每个规则地址都长这样，如果 SHA 在 CDN 上取不到，
    客户端会整包拉不到规则。宁可保持旧 ref，也不能写一个取不到的。
    直连 CDN 冷启动偶发 30s 级延迟，串行重试必须受总预算约束。
    """
    for i in range(tries):
        try:
            st, body = http_get(
                'https://fastly.jsdelivr.net/gh/%s@%s/rule/cn.yaml' % (repo, sha),
                timeout=30)
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
    """提交并推送。返回 (是否推送成功, 当前 HEAD 完整 SHA)。

    注意：「无改动」只代表不用新建提交，**不代表远端已经是最新的**。
    早期版本在这里直接 return，导致手工提交过的改动永远推不上去
    （远端落后 → ref_reachable 失败 → ref 卡在旧 commit）。所以这里
    无论如何都要走一次 push，git 会自己判断 up-to-date。
    """
    repo = gh['repo']
    run_git(['add', '-A'], timeout=120)

    diff = run_git(['status', '--porcelain'], timeout=60).stdout.decode('utf-8', 'replace')
    if not diff.strip():
        print('  无新改动，跳过提交（仍会推送本地已有提交）')
    else:
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

    r = run_git(['push', url, 'HEAD:main'], use_proxy=True, timeout=240)
    if r.returncode != 0:
        # 代理挂了就试直连（出国网络恰好可用时）
        r2 = run_git(['push', url, 'HEAD:main'], timeout=90)
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
        st, body = http_get(url, timeout=20)
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


def step_verify(conf, ref):
    """并行校验，并受全局时间预算约束。

    直连 jsDelivr 冷启动可能单请求 30s 级；此前 22 条 purge + 22 条校验
    全部串行、无总时限，父进程 600s 到点直接杀掉，连"哪些没校验完"都拿不到。
    现在每个请求都有短超时，且剩余时间不足时明确降级为未完成（返回 False）。
    """
    gh = conf['github']
    main_base = gh['rule_base']                       # @main（公开模板用）
    exp_base = main_base.replace('/rule', '/export')
    pin_base = _ref.rule_base(ref=ref) if ref else main_base

    print('  刷新 jsDelivr 缓存（export 与公开模板仍指向 @main）:')
    stat = {'ok': 0, 'throttled': 0, 'error': 0, 'skipped': 0}
    rels = ['rule/%s.yaml' % name for name in RULES] + ['export/%s' % name for name, _ in EXPORTS]
    remaining = _remaining()
    if remaining is not None and remaining < 60:
        stat['skipped'] = len(rels)
        print('    跳过：剩余时间不足（固定 ref 的客户端不依赖 purge）')
    else:
        for result in _parallel_map(rels, lambda rel: purge_path(gh['repo'], rel)):
            stat['skipped' if isinstance(result, Exception) else result] += 1
        print('    成功 %d  被节流 %d  失败 %d' % (stat['ok'], stat['throttled'], stat['error']))
        if stat['throttled']:
            print('    !! 有路径被节流，@main 上可能仍是旧内容（不影响已固定 ref 的客户端）')

    ok = bad = 0

    remaining = _remaining()
    if remaining is not None and remaining < 15:
        skipped = len(RULES) + len(EXPORTS)
        print('    跳过剩余校验：时间预算用尽（未完成 %d 项）' % skipped)
        print('  校验: 成功 0 / 失败 %d（预算用尽）' % skipped)
        return False

    print('  客户端实际拉取的地址（固定 ref）: %s' % pin_base)

    def check_rule(name):
        st, body = http_get('%s/%s.yaml' % (pin_base, name), timeout=30)
        head = body[:200].decode('utf-8', 'replace')
        good = (st == 200 and head.startswith('# bridge rule set'))
        n = head.split('count: ')[1].split(' ')[0] if 'count: ' in head else '?'
        note = 'OK' if good else '<<< 内容异常'
        lc = local_count(name)
        if good and lc and lc != n:
            note = '<<< 与本地不一致（本地 %s）' % lc
            good = False
        return good, '    %-12s HTTP=%s  %7s B  count=%s  %s' % (name, st, len(body), n, note)

    for name, result in zip(RULES, _parallel_map(RULES, check_rule)):
        if isinstance(result, Exception):
            print('    %-12s 失败 %s' % (name, str(result)[:60]))
            bad += 1
        else:
            good, line = result
            print(line)
            ok += 1 if good else 0
            bad += 0 if good else 1

    print('  导出文件校验（@main）:')

    def check_export(item):
        name, first = item
        st, body = http_get('%s/%s' % (exp_base, name), timeout=30)
        good = (st == 200 and body.startswith(first) and len(body) > 100)
        return good, '    %-24s HTTP=%s  %9s B  %s' % (
            name, st, len(body), 'OK' if good else '<<< 内容异常')

    for (name, _), result in zip(EXPORTS, _parallel_map(EXPORTS, check_export)):
        if isinstance(result, Exception):
            print('    %-24s 失败 %s' % (name, str(result)[:60]))
            bad += 1
        else:
            good, line = result
            print(line)
            ok += 1 if good else 0
            bad += 0 if good else 1

    remaining = _remaining()
    if remaining is not None and remaining < 5:
        print('  !! 时间预算用尽，校验结果可能不完整')
    print('  校验: 成功 %d / 失败 %d' % (ok, bad))
    return bad == 0


def main():
    do_push = '--no-push' not in sys.argv
    budget = DEFAULT_BUDGET
    for i, arg in enumerate(sys.argv):
        if arg == '--budget' and i + 1 < len(sys.argv):
            budget = sys.argv[i + 1]
        elif arg.startswith('--budget='):
            budget = arg.split('=', 1)[1]
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET
    set_budget(budget)

    conf = load_conf()
    gh = conf['github']

    print('=' * 56)
    print('bridge weekly  %s  预算 %ss' % (
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), budget))
    print('=' * 56)
    print('当前规则地址 ref: %s' % _ref.RULE_REF)

    print('\n[1/6] 拉取上游国内域名清单')
    step_fetch()

    print('\n[2/6] 重建规则集')
    stage = time.monotonic()
    step_build()

    print('\n[3/6] 导出通用规则订阅（不含节点）')
    step_export()
    print('  [构建+导出 %.1fs]' % (time.monotonic() - stage))

    ref = _ref.RULE_REF
    if do_push:
        print('\n[4/6] 推送规则到 GitHub')
        stage = time.monotonic()
        pushed, sha = step_push(gh, 'weekly: 规则更新 %s' % datetime.date.today().isoformat())
        print('  [推送 %.1fs]' % (time.monotonic() - stage))
        print('\n[5/6] 把规则地址固定到本次提交')
        # 判定只认"CDN 上真的读得到这个 commit"——它同时证明了推送成功、
        # 也证明了客户端将来能拉到。本次无改动时 HEAD 已在远端，同样成立。
        stage = time.monotonic()
        if sha and ref_reachable(sha, gh['repo']):
            try:
                changed = _ref.write_ref(
                    sha, note='weekly %s' % datetime.date.today().isoformat())
            except ValueError as e:
                changed = False
                print('  !! 写入 ref 失败：%s' % e)
            if changed:
                ref = sha
                print('  rule-ref.txt → %s，按新地址重建' % sha[:12])
                step_build()
                step_export()
            else:
                ref = _ref.read_ref()
                print('  ref 无变化（%s）' % (ref or '?')[:12])
        else:
            ref = _ref.read_ref()
            print('  !! 本次提交在 CDN 上还读不到，ref 保持不动（%s）' % (ref or '?')[:12])
            print('     客户端不受影响，继续用旧地址；脚本下次运行会再试')
        print('  [CDN 确认+ref 同步 %.1fs]' % (time.monotonic() - stage))
    else:
        print('\n[4/6] 跳过推送（--no-push）')
        print('\n[5/6] 跳过 ref 同步（--no-push）')

    if do_push:
        print('\n[6/6] 推送地址文件')
        stage = time.monotonic()
        step_push(gh, 'weekly: 规则地址指向 @%s' % (ref or '?')[:12])
        print('  [推送地址文件 %.1fs]' % (time.monotonic() - stage))
    else:
        print('\n[6/6] 跳过推送（--no-push）')

    print('\n校验')
    stage = time.monotonic()
    ok = step_verify(conf, ref)
    print('  [校验 %.1fs，剩余预算 %s]' % (
        time.monotonic() - stage,
        '不限' if _remaining() is None else '%.0fs' % _remaining()))

    print('\n结果: %s' % ('全部正常' if ok else '存在失败，需人工检查'))
    print('规则地址: %s' % _ref.rule_base(ref=ref))


if __name__ == '__main__':
    main()
