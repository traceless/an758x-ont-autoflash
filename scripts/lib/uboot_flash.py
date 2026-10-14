"""U-Boot 恢复页（192.168.0.1）HTTP API 封装。

接口（浏览器抓包确认）：
- POST /api/upload   原始二进制直传文件（无 Content-Type / 无 multipart 字段名），
                     返回 JSON，取 upload_id（无则取 id）
- POST /api/task     JSON 下发操作，返回 {"id": N}
- GET  /api/task?id=N 轮询任务状态（404 时降级 GET /api/task）
                     状态: {"id","state","busy","phase","code","output",...}

操作序列（顺序固定，每步必须 code==0 才继续）：
  flash-bl2 → ubi-rebuild → flash-board-data(bosa) + flash-board-data(ri)
  → flash-uboot → flash
  （ri 为新款 U-Boot 新增的板级数据项，MD/TF 都需要；bosa 与 ri 分两次
   上传+下发任务，请求体如 {"operation":"flash-board-data","target":"ri",
   "upload_id":N}）
"""
import json
import os
import time
import urllib.request
import urllib.error


class UbootError(RuntimeError):
    pass


def _request(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise UbootError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise UbootError(f"连接失败: {e.reason}") from None


def upload_file(host, path, timeout=300):
    """POST /api/upload 原始二进制直传。返回 upload_id。

    浏览器实测：请求不带 Content-Type，body 即文件字节流。
    若直传被拒，降级尝试 multipart（字段名 file/upload/firmware）。
    """
    with open(path, "rb") as f:
        data = f.read()
    url = f"http://{host}/api/upload"

    req = urllib.request.Request(url, data=data, method="POST")
    req.remove_header("Content-type")  # 去掉 urllib 默认的 form Content-Type
    req.add_header("Content-Length", str(len(data)))
    try:
        raw = _request(req, timeout)
    except UbootError:
        raw = None

    if raw is None:  # 降级 multipart
        import uuid
        for field in ("file", "upload", "firmware"):
            boundary = "----uboot" + uuid.uuid4().hex
            body = bytearray()
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; name="{field}"; '
                     f'filename="{os.path.basename(path)}"\r\n').encode()
            body += b"Content-Type: application/octet-stream\r\n\r\n"
            body += data + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=bytes(body), method="POST")
            req.add_header("Content-Type",
                           f"multipart/form-data; boundary={boundary}")
            try:
                raw = _request(req, timeout)
                break
            except UbootError:
                continue
    if raw is None:
        raise UbootError("上传失败：直传与 multipart 均被拒")

    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        raise UbootError(f"上传响应非 JSON: {raw[:200]!r}")
    uid = obj.get("upload_id", obj.get("id"))  # 成员取值而非 or，避免吞掉合法的 0
    if uid is None:
        raise UbootError(f"上传响应无 upload_id: {obj}")
    return uid


def start_task(host, operation, timeout=30, **params):
    """POST /api/task 下发操作，返回 task id。"""
    payload = {"operation": operation}
    payload.update(params)
    req = urllib.request.Request(
        f"http://{host}/api/task",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    raw = _request(req, timeout)
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        raise UbootError(f"task 响应非 JSON: {raw[:200]!r}")
    if "id" not in obj:
        raise UbootError(f"task 响应无 id: {obj}")
    return obj["id"]


def _get_task(host, task_id, timeout=10):
    """GET /api/task?id=N，404 降级 GET /api/task。"""
    try:
        raw = _request(urllib.request.Request(
            f"http://{host}/api/task?id={task_id}"), timeout)
    except UbootError as e:
        if "HTTP 404" in str(e):
            raw = _request(urllib.request.Request(
                f"http://{host}/api/task"), timeout)
        else:
            raise
    return json.loads(raw.decode("utf-8", "ignore"))


def wait_task(host, task_id, timeout=300, interval=2.0, label=""):
    """轮询任务直到 busy=false；code!=0 抛错。返回最终状态对象。

    设备执行擦除/写入时恢复页会短暂无响应（HTTP 超时），属正常现象，
    自动重试；连续 15 次（约 30s）无响应才追加升级提示。
    """
    deadline = time.time() + timeout
    last = None
    consec = 0  # 连续轮询失败次数（成功即清零）
    while time.time() < deadline:
        try:
            st = _get_task(host, task_id)
        except (UbootError, ValueError, OSError) as e:
            # OSError 兜底裸 socket.timeout/连接重置：设备擦写时恢复页会
            # accept 后挂起，urllib 抛的是 socket.timeout（OSError 子类但
            # 不是 URLError），不接住会击穿轮询容错误报任务失败
            consec += 1
            print(f"\r    设备正忙于擦除/写入，等待页面响应，自动重试中"
                  f"（第{consec}次）…", end="", flush=True)
            if consec == 15:
                print("\n    ⓘ 已连续多次无响应；若最终报「任务超时」，"
                      "可在菜单重新执行本步")
            time.sleep(interval)
            continue
        consec = 0
        last = st
        if not isinstance(st, dict):
            # 404 降级接口可能返回 JSON 数组等非 dict 结构
            raise UbootError(f"task 状态非预期结构: {st!r}")
        busy = st.get("busy", False)
        phase = st.get("phase", "")
        print(f"\r    [{label}] busy={busy} phase={phase}   ",
              end="", flush=True)
        if not busy:
            print()
            if st.get("code", 0) != 0:
                raise UbootError(
                    f"任务失败 code={st.get('code')} output={st.get('output')!r}")
            return st
        time.sleep(interval)
    raise UbootError(f"任务超时（{timeout}s）: {last}")


def run_step(host, label, upload_path=None, timeout=300, **task_params):
    """一步完整流程：可选上传 → 下发任务 → 等待完成。"""
    print(f"\n=== {label} ===")
    if upload_path:
        print(f"  上传 {os.path.basename(upload_path)} "
              f"({os.path.getsize(upload_path)} 字节)...")
        uid = upload_file(host, upload_path, timeout=timeout)
        print(f"  upload_id={uid}")
        task_params["upload_id"] = uid
    tid = start_task(host, **task_params)
    print(f"  task id={tid}，等待完成（超时 {timeout}s）...")
    st = wait_task(host, tid, timeout=timeout, label=label)
    out = (st.get("output") or "").strip()
    if out:
        print(f"  输出: {out[:300]}")
    print(f"  [OK] {label} 完成")
    return st


def try_reboot(host):
    """尽力触发重启（operation 名为猜测，失败不致命）。"""
    for op in ("reboot", "reset"):
        try:
            start_task(host, op)
            return True
        except UbootError:
            continue
    return False
