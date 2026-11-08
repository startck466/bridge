#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则集引用版本（ref）的唯一来源。

## 为什么规则地址不能写 @main

jsDelivr 对**分支引用**（@main）有边缘缓存，刷新只能靠 purge API，而 purge
按路径限流（返回 {"throttled":true} 时这次调用被直接丢弃，缓存根本没刷）。
2026-11-06 就因此卡住过：规则改对了、推上去了，客户端连续几小时拉到的还是旧内容。

固定 commit 的缓存键是全新的，push 完立刻可读，**完全不需要 purge**。

## rule-ref.txt 的写入时机

由 weekly.py 在「规则已推送成功」之后写入，内容就是那次提交的完整 SHA。
所以引用这个 ref 的一方拿到的必然是当周的规则。没有这个文件（或内容不合法）
时回退到 main，保证任何情况下都能构建出可用配置。
"""

import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REF_FILE = os.path.join(HERE, 'rule-ref.txt')

_SHA = re.compile(r'^[0-9a-f]{7,40}$')

USER = 'startck466'
REPO = 'bridge'
CDN_HOST = 'fastly.jsdelivr.net'


def read_ref(default='main'):
    """读 rule-ref.txt；缺失或格式不对时回退 default。"""
    try:
        with io.open(REF_FILE, encoding='utf-8') as f:
            v = f.read().strip()
    except (OSError, IOError):
        return default
    # 允许 "sha  # 说明" 这种写法：只取第一个 token
    v = v.split()[0] if v else ''
    return v if _SHA.match(v) else default


# 构建时用的 ref（模块加载即确定，三个生成脚本共用同一份）
RULE_REF = read_ref()


def rule_base(user=USER, repo=REPO, ref=None):
    """规则集 CDN 基址（不含结尾斜杠）。"""
    return 'https://%s/gh/%s/%s@%s/rule' % (CDN_HOST, user, repo, ref or RULE_REF)


def write_ref(sha, note=''):
    """写入新的 ref。内容没变返回 False。"""
    sha = (sha or '').strip()
    if not _SHA.match(sha):
        raise ValueError('不是合法的 commit SHA: %r' % sha)
    old = read_ref(default='')
    if old == sha:
        return False
    body = sha + ('\n' if not note else '  # %s\n' % note)
    with io.open(REF_FILE, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body)
    return True
