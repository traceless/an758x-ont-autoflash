"""开启光猫 telnet 服务（分机型，零第三方依赖）。

MD（移动）：POST http://{ip}/system.cgi?telnet+off → telnet+on（无 body），管理口 80。
TF（电信）：管理 CGI 在 :8080，访问 /system.cgi?telnet；端口/方法按 profile 探测，
           先试管理口再回退 80，最后以 telnet:23 能否建立并收到 banner 为准。

开 telnet 接口需要已登录的 Web 会话（传 opener）。Cookie 按主机名而非端口作用，
故登录在 80、管理 CGI 在 8080 也能带上会话。
"""
import socket
import time
import urllib.request


def _try_cgi(router_ip, port, method, path, opener=None, timeout=8, body=None):
    """向 {ip}:{port}/{path} 发一次请求，返回 (ok, detail)。

    body 为 None 时不带请求体（GET）；传 bytes 时作为 POST 请求体。
    """
    url = f"http://{router_ip}:{port}/{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-Requested-With", "XMLHttpRequest")
    req.add_header("Referer", f"http://{router_ip}:{port}/")
    if body is not None:
        req.add_header("Content-Type",
                       "application/x-www-form-urlencoded; charset=UTF-8")
    try:
        opener_obj = opener or urllib.request.build_opener()
        with opener_obj.open(req, timeout=timeout) as resp:
            resp.read()
            return True, f"{method} {url} -> HTTP {resp.getcode()}"
    except Exception as e:
        return False, f"{method} {url} 失败：{e}"


def _telnet_ready(router_ip, timeout=5):
    """telnet:23 能否建立并收到登录 banner（端口开但立刻断会返回 False）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((router_ip, 23))
        time.sleep(1)
        data = s.recv(1024)
        return bool(data)
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def open_telnet(profile, router_ip, opener=None, timeout=10):
    """按机型开启 telnet。返回 (ok, message)。profile 来自 lib.models。"""
    mode = profile.get("telnet_open", "cgi_toggle")
    mgmt = int(profile.get("mgmt_port", 80))

    if mode == "cgi_toggle":
        # MD：先 off 清残留会话，再 on 开启
        for act in ("telnet+off", "telnet+on"):
            ok, detail = _try_cgi(router_ip, mgmt, "POST", f"system.cgi?{act}",
                                  opener, timeout, body=b"")
            print(f"  [web] {detail}")
            time.sleep(1)
        if _telnet_ready(router_ip):
            return True, f"telnet 服务已开启（:{mgmt} POST telnet+on 生效）"
        return False, ("telnet 端口未开放（已 try off/on）。"
                       "请确认 Web 已登录且 system.cgi?telnet 可访问")

    # cgi_plain（TF，实测机制与 MD 同构）：管理 CGI 在 :8080 goahead，
    # POST /system.cgi?telnet+on（带 XHR 头与 body）即切换开关。
    # 先 off 清残留会话再 on；每发一次即校验 :23，命中即停。
    body = b"data&csrf_token="  # 设备页面 JS 的 ajaxSend 会补 csrf_token=（空值即可）
    for act in ("telnet+off", "telnet+on"):
        ok, detail = _try_cgi(router_ip, mgmt, "POST", f"system.cgi?{act}",
                              opener, timeout, body=body)
        print(f"  [web] {detail}")
        time.sleep(1)
        if _telnet_ready(router_ip):
            return True, f"telnet 服务已开启（:{mgmt} POST {act} 生效）"
    return False, ("telnet 端口未开放（已 POST telnet+off/on）。"
                   "请确认 Web 已登录，或浏览器访问 "
                   f"http://{router_ip}:{mgmt}/system.cgi?telnet 手动点开启")
