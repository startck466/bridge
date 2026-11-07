import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = "https://127.0.0.1:8443/v0/management/api-key-usage"
req = urllib.request.Request(url, headers={
    "Authorization": "Bearer HIcwcsx3qf9QKoKnuC3Y"
})

try:
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        print("=== CPA 网关供应商用量统计 ===")
        for provider, keys in data.items():
            print(f"\n[供应商: {provider}]")
            for endpoint, stats in keys.items():
                success = stats.get('success', 0)
                failed = stats.get('failed', 0)
                print(f"  端点: {endpoint}")
                print(f"  成功请求: {success} | 失败请求: {failed}")
                recent = stats.get('recent_requests', [])
                total_recent_succ = sum(r.get('success', 0) for r in recent)
                total_recent_fail = sum(r.get('failed', 0) for r in recent)
                print(f"  近期汇总 (最近窗口): 成功 {total_recent_succ}, 失败 {total_recent_fail}")
except Exception as e:
    print("Error querying management API:", e)
