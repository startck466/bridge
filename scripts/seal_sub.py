#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把含节点的订阅配置 AES-256-GCM 加密后写入仓库（只入库密文）。

流程:
    build_config() 生成明文 yaml
        -> AES-256-GCM（每次随机 12 字节 nonce）
        -> 输出 "BRIDGE-SUB-v1" + 单行 base64(nonce || ciphertext||tag)
        -> 写 sub/bridge.enc

解密在 Cloudflare Worker 里做（worker/sub-worker.js），密钥只存在于
        - 本地 scripts/.hosts.json（已 gitignore）
        - Cloudflare Worker 的 Secret 变量 SUB_KEY
    GitHub 仓库里只有密文，没有密钥，即使仓库公开也解不开。

用法:
    python3 seal_sub.py               # 生成 + 加密 + 写文件
    python3 seal_sub.py --print       # 额外打印明文前 8 行做自检（含节点，慎用）
"""

import io
import os
import sys
import json
import base64
import hashlib
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_sub import build_config, DEFAULT_BASE  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOSTS = os.path.join(HERE, '.hosts.json')
OUT = os.path.join(ROOT, 'sub', 'bridge.enc')
MAGIC = 'BRIDGE-SUB-v1'

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.stderr.write('缺少 cryptography: pip install cryptography\n')
    sys.exit(2)


def load_creds():
    with io.open(HOSTS, encoding='utf-8') as f:
        d = json.load(f)
    sub = d.get('subscription') or {}
    key_b64 = sub.get('aes_key_b64')
    if not key_b64:
        sys.stderr.write('scripts/.hosts.json 缺少 subscription.aes_key_b64\n')
        sys.exit(2)
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        sys.stderr.write('aes_key_b64 解出来不是 32 字节（AES-256 要求 32）\n')
        sys.exit(2)
    return d, key


def seal(plain, key):
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain.encode('utf-8'), MAGIC.encode('ascii'))
    blob = nonce + ct
    return '%s\n%s\n' % (MAGIC, base64.b64encode(blob).decode('ascii'))


def unseal(sealed, key):
    lines = sealed.strip().splitlines()
    if len(lines) < 2 or lines[0].strip() != MAGIC:
        raise ValueError('密文信封格式不对')
    blob = base64.b64decode(lines[1].strip())
    return AESGCM(key).decrypt(blob[:12], blob[12:], MAGIC.encode('ascii')).decode('utf-8')


def body_of(text):
    """去掉前几行注释（生成时间每次不同），只比正文。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith('#') or not lines[i].strip()):
        i += 1
    return '\n'.join(lines[i:])


def main():
    hosts, key = load_creds()
    base = (hosts.get('github') or {}).get('rule_base') or DEFAULT_BASE

    fresh = build_config(base)

    # 幂等：正文没变就不重写，避免每周产生无意义的密文 diff
    if os.path.isfile(OUT) and '--force' not in sys.argv:
        try:
            with io.open(OUT, encoding='utf-8') as f:
                old_plain = unseal(f.read(), key)
            if body_of(old_plain) == body_of(fresh):
                print('SKIP  正文未变，密文保持原样（%s）' % OUT)
                return
        except Exception as e:
            print('WARN  旧密文无法解析（%s），按新内容重写' % str(e)[:60])

    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    plain = fresh.replace(
        '# 生成时间: %s' % datetime.date.today().isoformat(),
        '# 生成时间: %s' % stamp, 1)

    sealed = seal(plain, key)

    d = os.path.dirname(OUT)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write(sealed)

    digest = hashlib.sha256(plain.encode('utf-8')).hexdigest()[:16]
    print('OK  密文 -> %s' % OUT)
    print('    明文 %d 字节 / 密文 %d 字节' % (len(plain), len(sealed)))
    print('    明文 sha256[:16] = %s' % digest)
    print('    路径令牌长度 = %d' % len((hosts.get('subscription') or {}).get('path_token', '')))

    if '--print' in sys.argv:
        print('--- 明文前 8 行 ---')
        for line in plain.splitlines()[:8]:
            print(line)


if __name__ == '__main__':
    main()
