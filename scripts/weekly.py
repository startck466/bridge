#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge 每周维护

流程:
  1. 经香港服务器拉取上游国内域名清单（本机直连 GitHub 不通）
  2. 重建全部规则集
  3. 上传到服务器规则分发目录
  4. 重新生成订阅配置并上传
  5. 全量校验（每个规则集必须 200）
  6. 若提供了 --github，则尝试推送 GitHub（失败不中断）

用法:
    python3 scripts/weekly.py            # 只更新服务器
    python3 scripts/weekly.py --github   # 同时尝试推 GitHub

服务器凭据从 scripts/.hosts.json 读取（已 gitignore，不入库）
"""

import io
import json
import os
import subprocess
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(BASE, 'scripts', '.hosts.json')
DATA = os.path.join(BASE, 'scripts', 'data')
RULE = os.path.join(BASE, 'rule')

UPSTREAM = {
    'direct.txt':
        'https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/direct-list.txt',
}

RULES = ['fix-direct', 'fix-proxy', 'ai', 'dev', 'social', 'media',
         'cloud', 'apple', 'cn']


def load_conf():
    if not os.path.isfile(CONF):
        sys.exit('缺少 %s（含服务器地址与凭据）' % CONF)
    return json.load(io.open(CONF, encoding='utf-8'))


def ssh(conf):
    import paramiko
    c = conf['server']
    j = paramiko.SSHClient()
    j.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    j.connect(c['host'], c.get('port', 22), c['user'], c['password'], timeout=25)
    return j


def run(j, script, timeout=600):
    chan = j.get_transport().open_session()
    chan.exec_command('bash -s')
    chan.sendall(script.encode())
    chan.shutdown_write()
    out = b''
    while True:
        if chan.recv_ready():
            out += chan.recv(65536)
        if chan.exit_status_ready() and not chan.recv_ready():
            break
    return out.decode('utf-8', 'replace'), chan.recv_exit_status()


def step_fetch(conf):
    """在服务器上拉上游清单，再取回本地（绕开本机网络限制）"""
    j = ssh(conf)
    cmds = []
    for name, url in UPSTREAM.items():
        cmds.append('curl -s --max-time 60 -o /root/_up_%s "%s" || echo FETCHFAIL:%s'
                    % (name, url, name))
        cmds.append('wc -l < /root/_up_%s' % name)
    out, _ = run(j, '\n'.join(cmds))
    lines = [l for l in out.splitlines() if l.strip()]
    counts = [l for l in lines if l.strip().isdigit()]
    print('  上游拉取:', ' '.join(lines[-len(UPSTREAM) * 2:])[:160])

    if not os.path.isdir(DATA):
        os.makedirs(DATA)
    sftp = j.open_sftp()
    for name in UPSTREAM:
        local = os.path.join(DATA, name)
        tmp = local + '.new'
        sftp.get('/root/_up_' + name, tmp)
        n = sum(1 for _ in io.open(tmp, encoding='utf-8', errors='replace'))
        old = 0
        if os.path.isfile(local):
            old = sum(1 for _ in io.open(local, encoding='utf-8', errors='replace'))
        # 上游文件行数骤降 20% 以上视为异常，保留旧数据
        if old and n < old * 0.8:
            print('  !! %s 上游行数 %d → %d 降幅过大，保留旧数据' % (name, old, n))
            os.remove(tmp)
        else:
            if os.path.exists(local):
                os.remove(local)
            os.rename(tmp, local)
            print('  %s  %d 行（旧 %d）' % (name, n, old))
    sftp.close()
    j.close()


def step_build():
    r = subprocess.run([sys.executable, os.path.join(BASE, 'scripts', 'build.py')],
                       cwd=BASE, capture_output=True)
    txt = r.stdout.decode('utf-8', 'replace')
    print('\n'.join('  ' + l for l in txt.strip().splitlines()[-12:]))
    if r.returncode != 0:
        sys.exit('构建失败：' + r.stderr.decode('utf-8', 'replace')[:400])


def step_deploy(conf, token, sub_token, rule_base):
    j = ssh(conf)
    out, _ = run(j, 'ls -d /root/bridge_http/%s /root/bridge_http_sub' % token)
    if 'No such' in out:
        sys.exit('服务器分发目录不存在，未部署过？')

    sftp = j.open_sftp()
    remote = '/root/bridge_http/' + token
    for name in RULES:
        sftp.put(os.path.join(RULE, name + '.yaml'), remote + '/' + name + '.yaml')

    # 订阅配置
    sub_out = os.path.join(BASE, 'sub', 'bridge-sub.yaml')
    r = subprocess.run([sys.executable, os.path.join(BASE, 'scripts', 'make_sub.py'),
                        '--base=' + rule_base, '--out=' + sub_out],
                       cwd=BASE, capture_output=True)
    if r.returncode != 0:
        sys.exit('订阅生成失败：' + r.stderr.decode('utf-8', 'replace')[:300])
    sftp.put(sub_out, '/root/bridge_http_sub/bridge.yaml')
    sftp.close()

    # 全量校验
    chk = ['T=%s' % token, 'S=%s' % sub_token, 'ok=0', 'bad=0']
    for name in RULES + ['sub']:
        if name == 'sub':
            url = 'https://startck466.us.ci:9443/$S/bridge.yaml'
        else:
            url = 'https://startck466.us.ci:9443/$T/%s.yaml' % name
        chk.append('c=$(curl -s -o /dev/null -w "%%{http_code}" --max-time 60 "%s")' % url)
        chk.append('echo "  %-12s $c"' % name)
        chk.append('if [ "$c" = "200" ]; then ok=$((ok+1)); else bad=$((bad+1)); fi')
    chk.append('echo "校验: 成功 $ok / 失败 $bad"')
    out, _ = run(j, '\n'.join(chk))
    print(out.strip())
    j.close()
    return '失败 0' in out


def main():
    do_github = '--github' in sys.argv
    conf = load_conf()
    sub = conf['dist']

    print('=' * 56)
    print('bridge weekly  %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
    print('=' * 56)

    print('\n[1/3] 拉取上游国内域名清单')
    step_fetch(conf)

    print('\n[2/3] 重建规则集')
    step_build()

    print('\n[3/3] 部署并校验')
    ok = step_deploy(conf, sub['rule_token'], sub['sub_token'], sub['rule_base'])
    print('\n分发结果: %s' % ('全部正常' if ok else '存在失败，需人工检查'))

    if do_github:
        print('\n[附加] 推送 GitHub')
        r = subprocess.run(['git', 'add', '-A'], cwd=BASE, capture_output=True)
        r = subprocess.run(['git', 'commit', '-m', 'weekly: 规则更新 %s'
                            % datetime.date.today().isoformat()],
                           cwd=BASE, capture_output=True)
        r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'],
                           cwd=BASE, capture_output=True)
        if r.returncode == 0:
            print('  GitHub 已更新')
        else:
            print('  GitHub 推送失败（不影响服务器分发）:',
                  r.stderr.decode('utf-8', 'replace').strip()[:200])


if __name__ == '__main__':
    main()
