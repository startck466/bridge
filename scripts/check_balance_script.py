#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_balance_script.py — CPA 网关用量统计

安全说明：
  - 本文件位于公开仓库 `startck466/bridge`，严禁包含任何明文密钥。
  - 管理密钥通过环境变量 `CPA_MGMT_KEY` 注入，或在运行时从本地
    `.workbuddy/secrets.md` 读取（不入库）。
"""
import os
import sys
import json
import ssl
import urllib.request


def _load_key():
    key = os.environ.get("CPA_MGMT_KEY")
    if key:
        return key
    # 本地开发时从 secrets.md 读取（该文件在 .gitignore 中，绝不入库）
    secrets_path = os.path.expanduser(
        "D:/ai/ai-vps/.workbuddy/secrets.md"
    )
    if os.path.isfile(secrets_path):
        import re
        with open(secrets_path, "r", encoding="utf-8") as f:
            m = re.search(r"管理密钥\*\*：`([^`]+)`", f.read())
            if m:
                return m.group(1)
    sys.exit("缺少环境变量 CPA_MGMT_KEY，且未在 secrets.md 中找到管理密钥。")


def main():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url = "https://127.0.0.1:8443/v0/management/api-key-usage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_load_key()}"
    })

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("=== CPA 网关供应商用量统计 ===")
            for provider, keys in data.items():
                print(f"\n[供应商: {provider}]")
                for endpoint, stats in keys.items():
                    success = stats.get("success", 0)
                    failed = stats.get("failed", 0)
                    print(f"  端点: {endpoint}")
                    print(f"  成功请求: {success} | 失败请求: {failed}")
                    recent = stats.get("recent_requests", [])
                    total_recent_succ = sum(r.get("success", 0) for r in recent)
                    total_recent_fail = sum(r.get("failed", 0) for r in recent)
                    print(f"  近期汇总 (最近窗口): 成功 {total_recent_succ}, 失败 {total_recent_fail}")
    except Exception as e:
        print("Error querying management API:", e)


if __name__ == "__main__":
    main()
