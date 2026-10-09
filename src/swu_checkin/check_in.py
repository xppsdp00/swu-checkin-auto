from datetime import datetime, timedelta, timezone
from getpass import getpass
import json
import os
import time
import traceback

import requests

from .get_info import get_dormitory, get_student_id, get_transition_today, get_token

STATUS_MESSAGES = {
    0: "今日无签到记录",
    1: "签到成功",
    2: "已签到",
    3: "登录失败",
    4: "网络错误或数据异常",
    5: "请假期间无需签到",
}

# 终态不重试：成功 / 已签到 / 请假
# 其余（无记录、登录失败、网络异常）可能是抖动，打满次数才算失败
RETRYABLE_STATUS = {0, 3, 4}
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 8

# 单次请求超时。以前在 main() 里写死 10 秒，但 GitHub runner 到校园网的链路
# 实测比本机慢约 5 倍（登录 9.9s vs 1.9s），提交那一步正好压在 10 秒线上，
# 表现为随机出现的 [4]（详见 2026-10-07 起连续三天的失败）。
# 实测：打卡成功的运行耗时 42~59s，失败的是 121~146s，差值正好是
# 「3 次尝试 × 顶满 10s 超时 + 24s 重试等待」——就是超时，不是接口报错。
# 放宽到 30 秒，最坏情况下三次尝试连休眠也就两分半，窗口内绰绰有余。
DEFAULT_TIMEOUT = 30

# 最近一次失败的真实原因。main() 会把它并进最后一行输出，
# 于是微信推送正文里直接能看到原因，不用再翻 Actions 日志。
_LAST_FAILURE = ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _note_failure(detail: str) -> None:
    """记录失败原因：既进 Actions 日志，也进微信推送正文。"""
    global _LAST_FAILURE

    _LAST_FAILURE = detail
    print(f"[失败] {detail}", flush=True)


def _log_failure(where: str, exc: BaseException) -> None:
    """把失败的真实原因写进日志。

    以前所有异常都被压成一句「网络错误或数据异常」，线上连续三天返回 [4]
    却完全看不出卡在哪一步（既不知道是超时、是接口报错，还是解析出错）。
    现在把异常类型、消息、traceback 和 HTTP 响应体都打出来。
    """
    detail = f"{type(exc).__name__}: {exc}".strip()
    response = getattr(exc, "response", None)
    if response is not None:
        detail = f"HTTP {response.status_code} {detail}"

    _note_failure(f"{where}｜{detail}")
    if response is not None:
        print(f"[失败] 响应体前 500 字：{response.text[:500]}", flush=True)
    traceback.print_exc()


def _check_vacation_enabled(token: str, timeout: int) -> bool:
    """检查当前是否处于已批准的请假期间。

    两点注意（都曾导致漏判，进而误打卡 → 自动销假）：

    1. GitHub Actions runner 的进程时区是 UTC，而 listSelfLeaveData 返回的
       kssj/jssj 是北京时间。若直接用 datetime.now() 与北京时间比较，会相差
       8 小时，使请假第一天的检测失效（例如请假当天 18:00 起，打卡在 21:07
       触发时会被判定为"未请假"）。这里统一换算到北京时间再比。
    2. 接口返回多条申请时，生效的那条未必排在 records[0]（最新一条可能是
       未审批/已驳回的）。因此遍历所有记录，取"当前正处于已同意假期内"的那条。
    """
    headers = {"fighter-auth-token": token}
    url = "https://of.swu.edu.cn/gateway/fighter-baida/api/xsqjxj/listSelfLeaveData?pageNum=1&pageSize=10"
    
    try:
        response = requests.get(url=url, headers=headers, timeout=timeout)
        data = response.json().get("data", {})
        records = data.get("records", [])
        
        if not records:
            return False
        
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)

        for record in records:
            if record.get("lcztmc") != "已同意":
                continue
            try:
                start = datetime.strptime(record["kssj"], "%Y-%m-%d %H:%M")
                end = datetime.strptime(record["jssj"], "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if start <= now <= end:
                return True

        return False
    except (requests.exceptions.RequestException, KeyError, ValueError):
        return False


def _parse_dormitory_data(dormitory_list: list) -> tuple[dict, str, str]:
    """
    从 getDormitory 返回的 columnList 解析签到数据
    返回: (位置信息, 宿舍楼名, 房间号)
    """
    location = None
    building = None
    room = None
    
    for item in dormitory_list:
        prop = item.get("prop", "")
        if prop == "qddz":
            location = {
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude")
            }
        elif not prop and item.get("latitude") is not None and item.get("longitude") is not None:
            # 2026-09: 接口格式变化，定位条目不再携带 prop=qddz，仅含经纬度和地址
            location = {
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude")
            }
        elif prop == "qsqddd":
            building = item.get("value")
        elif prop == "qdbj":
            room = item.get("value")
    
    if not all([location, building, room]):
        raise ValueError("宿舍信息不完整")
    
    return location, building, room


def _submit_checkin(token: str, timeout: int) -> int:
    """
    执行签到请求
    返回: 1=成功, 4=网络错误, None=无今日记录
    """
    try:
        transition = get_transition_today(token, timeout)
        if transition is None:
            return None
        
        form_id = transition["formId"]
        record_id = transition["id"]
        
        # 获取宿舍信息
        dorm_response = get_dormitory(token, timeout)
        column_list = dorm_response.get("data", {}).get("columnList", [])
        location, building, room = _parse_dormitory_data(column_list)
        
        headers = {
            "fighter-auth-token": token,
            "Content-Type": "application/json;charset=UTF-8"
        }
        url = "https://of.swu.edu.cn/gateway/fighter-baida/api/form-instance/save"
        params = {"formId": form_id, "isSubmitProcess": False}
        
        payload = {
            "id": record_id,
            "formId": form_id,
            "tsrq": time.strftime("%Y-%m-%d"),
            "xh": get_student_id(token, timeout),
            "qdsj": ["21:00", "23:30"],
            "qsqddd": building,
            "qdbj": room,
            "qddz": {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "address": building,
                "netType": "wifi",
                "operatorType": "unknown",
                "imei": "imei",
                "time": int(time.time() * 1000),
                "provider": "lbs",
                "isFromMock": False,
                "isGpsEnabled": True,
                "isWifiEnabled": True,
                "isMobileEnabled": False,
                "isOffset": True,
                "cityAdCode": "023",
                "districtAdCode": "500109",
                "isArea": True,
                "tip": "当前在签到范围内"
            }
        }
        
        started = time.time()
        response = requests.post(
            url,
            headers=headers,
            params=params,
            data=json.dumps(payload),
            timeout=timeout
        )
        elapsed = time.time() - started
        # 记录耗时：runner 到校园网的链路慢，这行能看出是否贴着 timeout 在跑
        print(f"[提交] HTTP {response.status_code}，耗时 {elapsed:.1f}s（timeout={timeout}s）", flush=True)
        response.raise_for_status()

        # 服务端也可能用 HTTP 200 + 业务码的方式拒绝（例如判定不在签到范围）。
        # 以前不看响应体，一律当成功，会把「被拒绝」误报成 [1]。
        try:
            body = response.json()
        except ValueError:
            return 1

        if isinstance(body, dict) and body.get("code") not in (None, 0, 200):
            _note_failure(f"服务端拒绝｜code={body.get('code')} msg={body.get('msg')}")
            print(f"[失败] 完整响应：{response.text[:500]}", flush=True)
            return 4

        return 1

    except requests.exceptions.HTTPError as exc:
        # raise_for_status()：服务端明确返回了 4xx/5xx
        _log_failure("接口返回错误状态", exc)
        return 4
    except requests.exceptions.RequestException as exc:
        # 连接失败、读写超时等传输层问题
        _log_failure("请求异常（超时/连接失败等）", exc)
        return 4
    except (KeyError, ValueError, TypeError) as exc:
        # 接口返回结构与代码预期不符
        _log_failure("返回数据结构不符预期", exc)
        return 4


def check_in(username: str, password: str, timeout: int = DEFAULT_TIMEOUT) -> int:
    """
    执行一次宿舍签到。

    返回值:
        0: 今日无签到记录
        1: 签到成功
        2: 已签到
        3: 登录失败
        4: 网络错误或数据异常
        5: 请假期间无需签到
    """
    try:
        token = get_token(username, password, timeout)
        if not token:
            return 3

        if _check_vacation_enabled(token, timeout):
            return 5

        transition = get_transition_today(token, timeout)
        if not transition:
            return 0

        if transition.get("qdzt") == "已签到":
            return 2

        result = _submit_checkin(token, timeout)
        if result is None:
            return 0

        return result
    except (KeyboardInterrupt, SystemExit):
        raise
    except (requests.exceptions.RequestException, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _log_failure("签到流程失败", exc)
        return 4
    except Exception as exc:
        _log_failure("签到流程出现未预期异常", exc)
        return 4


def check_in_with_retry(
    username: str,
    password: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int | None = None,
    retry_delay: int | None = None,
) -> int:
    """
    执行签到，瞬时失败自动重试。

    可通过环境变量覆盖：
        SWUDK_MAX_ATTEMPTS  总尝试次数，默认 3
        SWUDK_RETRY_DELAY   首次重试等待秒数，之后指数退避，默认 8
    """
    attempts = max_attempts or _env_int("SWUDK_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    delay = retry_delay or _env_int("SWUDK_RETRY_DELAY", DEFAULT_RETRY_DELAY)
    last_result = 4

    for attempt in range(1, attempts + 1):
        last_result = check_in(username, password, timeout)
        if last_result not in RETRYABLE_STATUS or attempt >= attempts:
            return last_result

        wait = delay * (2 ** (attempt - 1))
        reason = STATUS_MESSAGES.get(last_result, "未知状态")
        print(f"第 {attempt}/{attempts} 次失败（{reason}），{wait} 秒后重试")
        time.sleep(wait)

    return last_result


def main() -> int:
    username = os.getenv("SWUDK_USERNAME") or input("校园网账号：").strip()
    password = os.getenv("SWUDK_PASSWORD") or getpass("校园网密码：")
    timeout = _env_int("SWUDK_TIMEOUT", DEFAULT_TIMEOUT)
    result = check_in_with_retry(username, password, timeout)
    message = STATUS_MESSAGES.get(result, "未知状态")
    # 失败时把真实原因并进最后一行，微信推送正文即可看到，不必翻 Actions 日志。
    # 换行必须压平：这一行会被 workflow 原样写进 $GITHUB_OUTPUT，带换行会解析错。
    # 方括号也要换掉：workflow 用 grep -oP '\[\K[0-9]+(?=\])' 从这一行抠状态码，
    # 原因里若出现 [500] 之类会抠出第二个数，把状态码解析乱。
    if result not in {1, 2} and _LAST_FAILURE:
        detail = " ".join(_LAST_FAILURE.split())[:160]
        detail = detail.replace("[", "(").replace("]", ")")
        message = f"{message}（{detail}）"
    print(f"[{result}] {message}")
    return 0 if result in {1, 2} else 1


if __name__ == "__main__":
    raise SystemExit(main())