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
    python3 scripts/weekly.py --verify-only --skip-purge --budget 120
                                        # 只读校验22项，不读凭据、不构建、不写入、不推送

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
VERIFY_RESERVE = 120
MAX_HTTP_BYTES = 32 * 1024 * 1024
DEADLINE = 0.0

# socket 超时不是整次读取时限；独立进程让 DNS、TLS 和慢速响应都可终止。
# 不继承私有凭据，不将异常中的 URL / 代理配置输出到日志。
HTTP_WORKER = r'''
import gzip
import http.client
import io
import sys
import urllib.error
import urllib.request
try:
    request = urllib.request.Request(sys.argv[1], headers={
        'User-Agent': 'bridge-weekly/1.0', 'Accept-Encoding': 'gzip'})
    sys.stderr.write('open\n')
    sys.stderr.flush()
    with urllib.request.urlopen(request, timeout=float(sys.argv[2])) as response:
        sys.stderr.write('read\n')
        sys.stderr.flush()
        max_bytes = int(sys.argv[3])
        # HTTPResponse 已按 chunked 优先级解析长度；限长 read 不会自行拒绝提前 EOF。
        expected_length = getattr(response, 'length', None)
        if expected_length is not None and expected_length > max_bytes:
            raise ValueError('http_body_too_large')
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError('http_body_too_large')
        if expected_length is not None and len(body) != expected_length:
            raise http.client.IncompleteRead(body, expected_length - len(body))
        encoding = (response.getheader('Content-Encoding') or 'identity').strip().lower()
        if encoding == 'gzip':
            if not body:
                raise EOFError('empty_gzip_body')
            # 多读一个字节判定膨胀超限；到 EOF 才确认 CRC、尾部及后续 member 完整。
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError('http_body_too_large')
        elif encoding != 'identity':
            raise ValueError('http_unsupported_encoding')
        sys.stderr.write('write\n')
        sys.stderr.flush()
        sys.stdout.buffer.write(str(response.status).encode('ascii') + b'\n' + body)
        sys.stderr.write('flush\n')
        sys.stderr.flush()
        sys.stdout.buffer.flush()
        sys.stderr.write('done\n')
        sys.stderr.flush()
except urllib.error.HTTPError as exc:
    sys.stderr.write('http_status_%s' % exc.code)
    sys.exit(1)
except Exception as exc:
    reason = getattr(exc, 'reason', exc)
    message = str(exc) if str(exc) in ('http_body_too_large', 'http_unsupported_encoding') else type(reason).__name__
    sys.stderr.write(message)
    sys.exit(1)
'''


def set_budget(seconds):
    global DEADLINE
    seconds = int(seconds) if seconds else 0
    DEADLINE = time.monotonic() + seconds if seconds > 0 else 0.0


def _remaining():
    return None if not DEADLINE else DEADLINE - time.monotonic()


def _cap(timeout, reserve=0):
    rem = _remaining()
    limit = float(timeout) if rem is None else min(float(timeout), rem - reserve)
    if limit <= 0:
        raise TimeoutError('weekly_budget_exhausted')
    return limit


def _parallel_map(items, fn, workers=8, deadline=None):
    """保序并发；截止时逐项保留成功结果或明确超时，不等待未启动任务。"""
    deadline = deadline or DEADLINE or (time.monotonic() + DEFAULT_BUDGET)
    results = [TimeoutError('weekly_budget_exhausted') for _ in items]
    if time.monotonic() >= deadline:
        return results
    pool = ThreadPoolExecutor(max_workers=workers)

    def bounded(item):
        if time.monotonic() >= deadline:
            raise TimeoutError('weekly_budget_exhausted')
        return fn(item)

    try:
        futures = {pool.submit(bounded, item): i for i, item in enumerate(items)}
        try:
            for fut in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
                index = futures[fut]
                try:
                    results[index] = fut.result()
                except Exception as exc:  # noqa: BLE001 - 单项失败不能中断其余项
                    results[index] = exc
        except TimeoutError:
            pass
        for fut, index in futures.items():
            if fut.done() and not fut.cancelled():
                try:
                    results[index] = fut.result()
                except Exception as exc:
                    results[index] = exc
            else:
                fut.cancel()
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
    limit = _cap(timeout)
    # 仅保留运行 Python、系统证书和现有网络通道所需的环境。
    allowed = {'path', 'systemroot', 'windir', 'temp', 'tmp', 'home', 'userprofile',
               'ssl_cert_file', 'ssl_cert_dir', 'http_proxy', 'https_proxy',
               'all_proxy', 'no_proxy'}
    env = {key: value for key, value in os.environ.items() if key.lower() in allowed}
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', HTTP_WORKER, url, str(limit), str(MAX_HTTP_BYTES)],
            capture_output=True, timeout=_cap(limit), env=env)
    except subprocess.TimeoutExpired as exc:
        stages = (exc.stderr or b'').splitlines()
        stage = next((item.decode('ascii') for item in reversed(stages)
                      if item in (b'open', b'read', b'write', b'flush', b'done')), 'open')
        raise TimeoutError('http_total_timeout:' + stage) from None
    if result.returncode:
        reason = (result.stderr.decode('ascii', 'replace').strip() or 'worker_failed').splitlines()[-1]
        raise OSError('http_request_failed: ' + reason[:80])
    status, body = result.stdout.split(b'\n', 1)
    return int(status), body


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
                           cwd=BASE, capture_output=True,
                           timeout=_cap(timeout, reserve=VERIFY_RESERVE))
    except (subprocess.TimeoutExpired, TimeoutError):
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
                timeout=_cap(30, reserve=VERIFY_RESERVE))
            if st == 200 and body[:40].startswith(b'# bridge rule set'):
                return True
        except Exception:
            pass
        if i < tries - 1:
            remaining = _remaining()
            if remaining is not None and remaining <= VERIFY_RESERVE + 5:
                break
            time.sleep(5)
    return False


def run_git(args, use_proxy=False, timeout=300):
    cmd = ['git']
    if use_proxy:
        cmd += ['-c', 'http.proxy=' + GIT_PROXY, '-c', 'https.proxy=' + GIT_PROXY]
    cmd += args
    try:
        return subprocess.run(cmd, cwd=BASE, capture_output=True,
                              timeout=_cap(timeout, reserve=VERIFY_RESERVE), env=git_env())
    except (subprocess.TimeoutExpired, TimeoutError):
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
def purge_path(repo, rel, timeout=20):
    """刷新单条 jsDelivr 缓存。

    返回 'ok' / 'throttled' / 'error'。jsDelivr 对同一路径按分钟级节流，
    被节流时调用会被直接丢弃（缓存不会刷新），必须显式识别出来。

    注意：客户端的规则集已固定到 commit，**不依赖**这里刷得成不成功；
    这一步只为 export/ 与公开模板（仍引用 @main）服务。
    """
    url = 'https://purge.jsdelivr.net/gh/%s@main/%s' % (repo, rel)
    key = '/gh/%s@main/%s' % (repo, rel)
    try:
        st, body = http_get(url, timeout=timeout)
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


def _verify_request_timeout(path, deadline):
    try:
        large = os.path.getsize(path) >= 1024 * 1024
    except OSError:
        large = False
    return _cap(min(60 if large else 30, deadline - time.monotonic()))


def step_verify(conf, ref, purge=True):
    """并行校验，并受全局时间预算约束。

    直连 jsDelivr 冷启动可能单请求 30s 级；此前 22 条 purge + 22 条校验
    全部串行、无总时限，父进程 600s 到点直接杀掉，连"哪些没校验完"都拿不到。
    purge 只消耗校验预留以外的时间；22 项共享截止时间和并发队列。
    逐项输出失败原因，任何失败或未完成都返回 False。
    """
    gh = conf['github']
    main_base = gh['rule_base']                       # @main（公开模板用）
    exp_base = main_base.replace('/rule', '/export')
    pin_base = _ref.rule_base(ref=ref) if ref else main_base
    print('  刷新 jsDelivr 缓存（export 与公开模板仍指向 @main）:')
    stat = {'ok': 0, 'throttled': 0, 'error': 0, 'skipped': 0}
    rels = ['rule/%s.yaml' % name for name in RULES] + ['export/%s' % name for name, _ in EXPORTS]
    remaining = _remaining()
    if not purge or (remaining is not None and remaining <= VERIFY_RESERVE + 20):
        stat['skipped'] = len(rels)
        print('    跳过：只读校验或为22项校验预留时间（固定 ref 不依赖 purge）')
    else:
        purge_deadline = time.monotonic() + 20
        if DEADLINE:
            purge_deadline = min(purge_deadline, DEADLINE - VERIFY_RESERVE)
        for result in _parallel_map(
                rels, lambda rel: purge_path(gh['repo'], rel,
                                             timeout=purge_deadline - time.monotonic()),
                deadline=purge_deadline):
            stat['skipped' if isinstance(result, Exception) else result] += 1
        print('    成功 %d  被节流 %d  失败 %d  未完成 %d' % (
            stat['ok'], stat['throttled'], stat['error'], stat['skipped']))
        if stat['throttled']:
            print('    !! 有路径被节流，@main 上可能仍是旧内容（不影响已固定 ref 的客户端）')

    verify_deadline = time.monotonic() + VERIFY_RESERVE
    if DEADLINE:
        verify_deadline = min(verify_deadline, DEADLINE)
    ok = bad = 0

    print('  客户端实际拉取的地址（固定 ref）: %s' % pin_base)

    def check_rule(name):
        st, body = http_get('%s/%s.yaml' % (pin_base, name),
                            timeout=_verify_request_timeout(
                                os.path.join(RULE, name + '.yaml'), verify_deadline))
        head = body[:200].decode('utf-8', 'replace')
        good = (st == 200 and head.startswith('# bridge rule set'))
        n = head.split('count: ')[1].split(' ')[0] if 'count: ' in head else '?'
        note = 'OK' if good else '<<< 内容异常'
        lc = local_count(name)
        if good and lc and lc != n:
            note = '<<< 与本地不一致（本地 %s）' % lc
            good = False
        return good, '    %-12s HTTP=%s  %7s B  count=%s  %s' % (name, st, len(body), n, note)

    def check_export(item):
        name, first = item
        st, body = http_get('%s/%s' % (exp_base, name),
                            timeout=_verify_request_timeout(
                                os.path.join(BASE, 'export', name), verify_deadline))
        good = (st == 200 and body.startswith(first) and len(body) > 100)
        return good, '    %-24s HTTP=%s  %9s B  %s' % (
            name, st, len(body), 'OK' if good else '<<< 内容异常')

    checks = [('rule', name) for name in RULES] + [('export', item) for item in EXPORTS]
    results = _parallel_map(
        checks, lambda item: check_rule(item[1]) if item[0] == 'rule' else check_export(item[1]),
        deadline=verify_deadline)
    # 仅恢复瞬时传输失败；内容、CRC、截断等错误绝不重试。
    transient = ('http_total_timeout:', 'http_request_failed: TimeoutError',
                 'http_request_failed: ConnectionResetError',
                 'http_request_failed: ConnectionAbortedError')
    retry_indices = [i for i, result in enumerate(results)
                     if isinstance(result, (TimeoutError, OSError))
                     and str(result).startswith(transient)]
    if retry_indices and verify_deadline - time.monotonic() >= 5:
        retry_checks = [checks[i] for i in retry_indices]
        print('  瞬时传输失败最多重试一次：%d项（仍受本轮截止时间限制）' % len(retry_checks))
        retries = _parallel_map(
            retry_checks,
            lambda item: check_rule(item[1]) if item[0] == 'rule' else check_export(item[1]),
            deadline=verify_deadline)
        for index, result in zip(retry_indices, retries):
            previous = results[index]
            results[index] = result
            label = checks[index][1]
            label = label if isinstance(label, str) else label[0]
            recovered = not isinstance(result, Exception) and result[0]
            print('    重试 %s: 首次=%s；最终=%s' % (
                label, str(previous)[:80], '恢复并完整校验通过' if recovered else '仍失败'))
    for (kind, item), result in zip(checks, results):
        name = item if kind == 'rule' else item[0]
        if kind == 'export' and item == EXPORTS[0]:
            print('  导出文件校验（@main）:')
        if isinstance(result, Exception):
            print('    %-24s 失败 %s' % (name, str(result)[:80]))
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
    if budget <= 0:
        print('预算必须是正整数')
        return 2
    set_budget(budget)

    if '--verify-only' in sys.argv:
        # 与 charon_weekly.run_bridge 的 HTTP 环境一致，避免手动验收走残留代理。
        # 仅改变当前进程；不修改系统代理，也不影响独立的 Git 显式代理。
        for key in list(os.environ):
            if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                os.environ.pop(key, None)
        os.environ['NO_PROXY'] = '*'
        print('  HTTP验收路径: 直连（与正式周检一致；系统代理未修改）')
        # 公开坐标由 _ref 提供；不加载 .hosts.json 或保险库环境中的凭据。
        conf = {'github': {'repo': '%s/%s' % (_ref.USER, _ref.REPO),
                           'rule_base': _ref.rule_base(ref='main')}}
        ok = step_verify(conf, _ref.read_ref(), purge='--skip-purge' not in sys.argv)
        return 0 if ok else 1

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
    export_ok = step_export()
    publish_ok = True
    print('  [构建+导出 %.1fs]' % (time.monotonic() - stage))

    ref = _ref.RULE_REF
    if do_push:
        print('\n[4/6] 推送规则到 GitHub')
        stage = time.monotonic()
        pushed, sha = step_push(gh, 'weekly: 规则更新 %s' % datetime.date.today().isoformat())
        publish_ok = pushed
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
                publish_ok = False
            if changed:
                ref = sha
                print('  rule-ref.txt → %s，按新地址重建' % sha[:12])
                step_build()
                export_ok = step_export() and export_ok
            else:
                ref = _ref.read_ref()
                print('  ref 无变化（%s）' % (ref or '?')[:12])
        else:
            publish_ok = False
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
        pushed, _ = step_push(gh, 'weekly: 规则地址指向 @%s' % (ref or '?')[:12])
        publish_ok = pushed and publish_ok
        print('  [推送地址文件 %.1fs]' % (time.monotonic() - stage))
    else:
        print('\n[6/6] 跳过推送（--no-push）')

    print('\n校验')
    stage = time.monotonic()
    ok = step_verify(conf, ref)
    print('  [校验 %.1fs，剩余预算 %s]' % (
        time.monotonic() - stage,
        '不限' if _remaining() is None else '%.0fs' % _remaining()))

    ok = ok and export_ok and publish_ok
    print('\n结果: %s' % ('全部正常' if ok else '存在失败，需人工检查'))
    print('规则地址: %s' % _ref.rule_base(ref=ref))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
