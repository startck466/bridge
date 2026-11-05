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


def main():
    hosts, key = load_creds()
    base = (hosts.get('github') or {}).get('rule_base') or DEFAULT_BASE

    plain = build_config(base)

    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    plain = plain.replace(
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
