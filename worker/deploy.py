#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署 / 更新 Cloudflare Worker「bridge-sub」并绑定自定义域。

凭据读 scripts/.hosts.json（已 gitignore）：
  cloudflare.{api_token, account_id, zone_id}
  subscription.{aes_key_b64, path_token, worker_name, worker_domain}

只依赖标准库（urllib），不需要 requests / wrangler。

用法:
    python3 worker/deploy.py              # 上传脚本 + 绑定域名
    python3 worker/deploy.py --script-only # 只更新脚本，不动域名
"""

import io
import os
import sys
import json
import uuid
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOSTS = os.path.join(ROOT, 'scripts', '.hosts.json')
SCRIPT = os.path.join(HERE, 'sub-worker.js')

API = 'https://api.cloudflare.com/client/v4'


def load():
    with io.open(HOSTS, encoding='utf-8') as f:
        d = json.load(f)
    cf = d.get('cloudflare') or {}
    sub = d.get('subscription') or {}
    missing = [k for k in ('api_token', 'account_id') if not cf.get(k)]
    if missing:
        sys.exit('scripts/.hosts.json 缺 cloudflare.%s' % '/'.join(missing))
    for k in ('aes_key_b64', 'path_token'):
        if not sub.get(k):
            sys.exit('scripts/.hosts.json 缺 subscription.%s' % k)
    return cf, sub


def call(method, path, token, body=None, ctype='application/json'):
    url = API + path
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('Content-Type', ctype)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return json.loads(raw)
        except Exception:
            return {'success': False, 'errors': [{'message': raw[:300]}]}


def multipart(fields, files):
    """fields: [(name, str_value, ctype_or_None)]，files: [(name, filename, bytes, ctype)]"""
    b = '----bridge' + uuid.uuid4().hex
    out = bytearray()
    for name, val, ctype in fields:
        out += ('--%s\r\n' % b).encode()
        head = 'Content-Disposition: form-data; name="%s"' % name
        if ctype:
            head += '\r\nContent-Type: %s' % ctype
        out += (head + '\r\n\r\n').encode()
        out += val.encode('utf-8') + b'\r\n'
    for name, filename, content, ctype in files:
        out += ('--%s\r\n' % b).encode()
        out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name, filename)).encode()
        out += ('Content-Type: %s\r\n\r\n' % ctype).encode()
        out += content + b'\r\n'
    out += ('--%s--\r\n' % b).encode()
    return bytes(out), 'multipart/form-data; boundary=' + b


def upload(cf, sub):
    with io.open(SCRIPT, 'rb') as f:
        code = f.read()

    metadata = {
        'main_module': 'sub-worker.js',
        'compatibility_date': '2025-09-01',
        'bindings': [
            {'type': 'secret_text', 'name': 'SUB_KEY', 'text': sub['aes_key_b64']},
            {'type': 'secret_text', 'name': 'SUB_PATH', 'text': sub['path_token']},
        ],
        'observability': {'enabled': False},
    }

    body, ctype = multipart(
        [('metadata', json.dumps(metadata), 'application/json')],
        [('sub-worker.js', 'sub-worker.js', code, 'application/javascript+module')],
    )
    name = sub.get('worker_name') or 'bridge-sub'
    r = call('PUT', '/accounts/%s/workers/scripts/%s' % (cf['account_id'], name),
             cf['api_token'], body, ctype)
    if not r.get('success'):
        return False, json.dumps(r.get('errors'), ensure_ascii=False)
    return True, 'script uploaded'


def bind_domain(cf, sub):
    host = sub['worker_domain']
    zone_id = None
    for suffix, zid in (cf.get('zone_id') or {}).items():
        if host == suffix or host.endswith('.' + suffix):
            zone_id = zid
            break
    if not zone_id:
        return False, '找不到 %s 对应的 zone_id' % host

    name = sub.get('worker_name') or 'bridge-sub'
    r = call('PUT', '/accounts/%s/workers/domains' % cf['account_id'], cf['api_token'], {
        'environment': 'production',
        'hostname': sub['worker_domain'],
        'service': name,
        'zone_id': zone_id,
    })
    if not r.get('success'):
        errs = json.dumps(r.get('errors'), ensure_ascii=False)
        if 'already' in errs or 'conflict' in errs.lower():
            return True, 'domain already bound'
        return False, errs
    return True, 'domain bound -> %s' % sub['worker_domain']


def main():
    cf, sub = load()

    ok, msg = upload(cf, sub)
    print('[script] %s  %s' % ('OK ' if ok else 'FAIL', msg))
    if not ok:
        sys.exit(1)

    if '--script-only' in sys.argv:
        return

    ok, msg = bind_domain(cf, sub)
    print('[domain] %s  %s' % ('OK ' if ok else 'FAIL', msg))
    if ok:
        print('\n订阅地址: https://%s/%s' % (sub['worker_domain'], sub['path_token']))


if __name__ == '__main__':
    main()
