"""光猫 Web 登录 + 开启 telnet 服务（纯标准库 urllib）。

实测要点：
- POST /login.cgi 需带 X-Requested-With: XMLHttpRequest 头，明文提交
- 表单字段名是 name / pswd（不是 username / password）
- 登录成功返回 299 + Set-Cookie sid
- 光猫有登录失败计数器：多次失败后返回 err=次数,剩余秒数 cookie 并锁定
- 开 telnet：带 session POST system.cgi?telnet
"""
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import re
import time


def _open(router_ip, timeout=10):
    """构建带 Cookie 的 opener，返回 (opener, cookiejar)。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("X-Requested-With", "XMLHttpRequest"),
        ("Referer", f"http://{router_ip}/"),
    ]
    return opener, cj, timeout


def _check_err_cookie(cj):
    """检查 err cookie，返回 (error_count, lock_seconds) 或 None。"""
    for c in cj:
        if c.name == "err":
            parts = c.value.split(",")
            if len(parts) >= 2:
                try:
                    return int(parts[0]), int(parts[1])
                except ValueError:
                    return int(parts[0]) if parts[0].isdigit() else None, None
    return None


def _try_login(opener, cj, router_ip, username, password, timeout):
    """单次登录尝试，返回 (code, body, has_sid)。"""
    data = urllib.parse.urlencode({
        "name": username,
        "pswd": password,
    }).encode()
    req = urllib.request.Request(
        f"http://{router_ip}/login.cgi", data=data, method="POST"
    )
    req.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        resp = opener.open(req, timeout=timeout)
        code = resp.getcode()
        body = resp.read().decode("utf-8", errors="replace")
        # 打印所有 Set-Cookie 头
        for h in resp.headers.get_all("Set-Cookie") or []:
            print(f"  [web] Set-Cookie: {h}")
    except urllib.error.HTTPError as e:
        code = e.code
        body = e.read().decode("utf-8", errors="replace")
        for h in e.headers.get_all("Set-Cookie") or []:
            print(f"  [web] Set-Cookie: {h}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接失败：{e.reason}")

    has_sid = any(c.name == "sid" for c in cj)
    return code, body, has_sid


def web_logout(router_ip, opener, timeout=10):
    """退出登录，注销服务端 session。

    接口：GET /login.cgi?out（带当前 opener 的 cookie session）。
    注意：URL 是 ?out 不带等号。
    """
    import subprocess

    cookies = {}
    for h in opener.handlers:
        if hasattr(h, 'cookiejar'):
            for c in h.cookiejar:
                cookies[c.name] = c.value
            break
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null",
                "-H", f"Cookie: {cookie_str}",
                "--max-time", str(timeout),
                f"http://{router_ip}/login.cgi?out",
            ],
            capture_output=True, text=True, timeout=timeout + 5
        )
        print("  [web] 已退出登录")
        return True
    except Exception as e:
        print(f"  [web] 退出登录异常：{e}")
        return False


def web_login(router_ip, username, password, timeout=10, retries=3, retry_delay=10):
    """Web 登录光猫，返回 (opener, cookiejar) 或抛异常。

    自动检测登录锁定（err cookie），提示等待时间。
    注意：用完后请调用 web_logout 退出，避免遗留会话。
    """
    opener, cj, t = _open(router_ip, timeout)

    # 先 GET 首页，拿 lang cookie 初始化
    try:
        opener.open(f"http://{router_ip}/", timeout=t).read()
    except Exception:
        pass

    for attempt in range(1, retries + 1):
        code, body, has_sid = _try_login(
            opener, cj, router_ip, username, password, t
        )

        if has_sid or code == 299:
            # 登录成功后访问首页，获取 -goahead-session- 和 logintoken 等 cookie
            try:
                opener.open(f"http://{router_ip}/", timeout=t).read()
            except Exception:
                pass
            # 打印所有 cookie，确认完整性
            all_cookies = [(c.name, c.value[:20]) for c in cj]
            print(f"  [web] cookies: {all_cookies}")
            return opener, cj

        # 检查是否被锁定（err cookie）
        err_info = _check_err_cookie(cj)
        if err_info:
            err_count, lock_sec = err_info
            if lock_sec and lock_sec > 0:
                raise RuntimeError(
                    f"光猫登录锁定保护已触发（失败 {err_count} 次），"
                    f"请等待约 {lock_sec} 秒（{lock_sec // 60} 分 {lock_sec % 60} 秒）后重试。"
                    f"\n  期间请勿反复尝试，否则会延长锁定时间。"
                )

        # 200 + 登录页 = 会话冲突或密码错误，等待重试
        if code == 200 and attempt < retries:
            print(f"  [web] 登录未成功（HTTP {code}），{retry_delay}s 后重试 "
                  f"({attempt}/{retries})...")
            print("  提示：光猫只允许一个管理会话，请在浏览器退出登录后再跑脚本")
            time.sleep(retry_delay)
            opener, cj, t = _open(router_ip, timeout)
            try:
                opener.open(f"http://{router_ip}/", timeout=t).read()
            except Exception:
                pass
            continue

    raise RuntimeError(
        f"登录失败：重试 {retries} 次仍未拿到 session。"
        f"最后 HTTP {code}，响应 {body[:200]}"
    )


def enable_telnet(router_ip, opener, cj, timeout=10):
    """通过 Web 接口开启 telnet 服务，返回 (success, message)。

    先 off 再 on，清除残留 telnet 会话。
    接口：POST /system.cgi?telnet+off / telnet+on（无 body 参数）。
    """
    import time

    for action in ["telnet+off", "telnet+on"]:
        req = urllib.request.Request(
            f"http://{router_ip}/system.cgi?{action}", data=b"", method="POST"
        )
        try:
            resp = opener.open(req, timeout=timeout)
            resp.read()
        except Exception as e:
            return False, f"{action} 请求失败：{e}"
        time.sleep(1)

    # 验证：不仅端口要开，还要能收到数据（防端口开但连接立刻断）
    import socket
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((router_ip, 23))
        time.sleep(1)
        data = s.recv(1024)
        s.close()
        if data:
            return True, f"telnet 服务已开启（收到 {len(data)} 字节登录提示）"
        else:
            return False, "telnet 端口开放但连接立刻断开（尝试再跑一次 enable-telnet）"
    except OSError as e:
        return False, f"telnet 端口未开放：{e}"
