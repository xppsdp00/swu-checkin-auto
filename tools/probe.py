# -*- coding: utf-8 -*-
"""只读探针：在 GitHub runner 上逐步复现打卡流程，打印每一步的真实结果。

不提交任何签到 —— 只做读操作，用来定位 [4] 到底出在哪一步。
用法：Actions → 诊断探针 → Run workflow（或 API 触发）
"""
import json
import os
import sys
import time
import traceback

import requests

from swu_checkin.get_info import (
    get_dormitory,
    get_student_id,
    get_token,
    get_transition_today,
)

TIMEOUT = 15
u = os.environ["SWUDK_USERNAME"]
p = os.environ["SWUDK_PASSWORD"]


def bj() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 8 * 3600))


def step(name, fn):
    print(f"\n=== {name} ===", flush=True)
    t0 = time.time()
    try:
        r = fn()
        print(f"  OK ({time.time() - t0:.1f}s)", flush=True)
        return r
    except Exception:
        print(f"  ✗ 异常 ({time.time() - t0:.1f}s)", flush=True)
        traceback.print_exc()
        return None


print("=" * 60)
print(f"runner UTC  : {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"北京时间    : {bj()}")
print(f"python      : {sys.version.split()[0]}")
print(f"出口 IP     : {step('出口 IP', lambda: requests.get('https://api.ipify.org', timeout=TIMEOUT).text)}")
print("=" * 60)

token = step("1. get_token（登录）", lambda: get_token(u, p, TIMEOUT))
print(f"  token: {'<非空 len=%d>' % len(token) if token else '<空!> 登录失败>'}", flush=True)
if not token:
    sys.exit("登录失败，探针到此结束")

tr = step("2. get_transition_today（今日任务）", lambda: get_transition_today(token, TIMEOUT))
print(f"  transition: {json.dumps(tr, ensure_ascii=False) if tr else tr}", flush=True)

if tr:
    print("\n  --- 直接裸请求一次，看 HTTP 层 ---", flush=True)
    try:
        raw = requests.post(
            "https://of.swu.edu.cn//gateway/fighter-baida/api/cqtj/getTransitionByToday",
            headers={"fighter-auth-token": token},
            data={"pageNum": 1, "pageSize": 1},
            timeout=TIMEOUT,
        )
        print(f"  HTTP {raw.status_code}  body={raw.text[:300]}", flush=True)
    except Exception:
        traceback.print_exc()

dorm = step("3. get_dormitory（宿舍信息）", lambda: get_dormitory(token, TIMEOUT))
print(f"  columnList: {json.dumps((dorm or {}).get('data', {}).get('columnList', []), ensure_ascii=False)[:600]}", flush=True)

sid = step("4. get_student_id", lambda: get_student_id(token, TIMEOUT))
print(f"  student_id: {'<非空 len=%d>' % len(sid) if sid else sid}", flush=True)

print("\n" + "=" * 60)
print("探针结束 —— 以上每一步都 OK 的话，问题就在最后那个 POST")
print("=" * 60)
