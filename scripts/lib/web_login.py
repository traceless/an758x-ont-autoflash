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


def _open(router_ip, timeout=10, port=80):
    """构建带 Cookie 的 opener，返回 (opener, cookiejar, timeout)。

    port 为 Web 登录端口（MD/TF 通常都是 80）；Cookie 按主机名作用，跨端口仍有效。
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("X-Requested-With", "XMLHttpRequest"),
        ("Referer", f"http://{router_ip}:{port}/"),
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


def _try_login(opener, cj, router_ip, username, password, timeout, port=80):
    """单次登录尝试，返回 (code, body, has_sid)。"""
    data = urllib.parse.urlencode({
        "name": username,
        "pswd": password,
    }).encode()
    req = urllib.request.Request(
        f"http://{router_ip}:{port}/login.cgi", data=data, method="POST"
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


def web_logout(router_ip, opener, timeout=10, port=80):
    """退出登录，注销服务端 session。

    接口：GET /login.cgi?out（带当前 opener 的 cookie session）。
    注意：URL 是 ?out 不带等号。
    纯标准库实现（原 curl 子进程方案在无 curl 环境会静默失败）。
    不跟随重定向：注销接口返回 302 携带 session 清除 Set-Cookie，
    自动跟随会丢失该头导致会话未真正注销。
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # 触发 HTTPError，保留原始 302 响应头

    no_redir = urllib.request.build_opener(_NoRedirect)
    for h in opener.handlers:
        if hasattr(h, "cookiejar"):
            no_redir.add_handler(h)  # 复用当前会话 cookie
            break
    try:
        with no_redir.open(f"http://{router_ip}:{port}/login.cgi?out",
                           timeout=timeout) as resp:
            resp.read()
        print("  [web] 已退出登录")
        return True
    except urllib.error.HTTPError:
        print("  [web] 已退出登录")  # 302 是注销接口的预期响应
        return True
    except Exception as e:
        print(f"  [web] 退出登录异常：{e}")
        return False


def web_login(router_ip, username, password, timeout=10, retries=3, retry_delay=10, port=80):
    """Web 登录光猫，返回 (opener, cookiejar) 或抛异常。

    自动检测登录锁定（err cookie），提示等待时间。
    port 为 Web 登录端口（默认 80）。
    注意：用完后请调用 web_logout 退出，避免遗留会话。
    """
    base = f"http://{router_ip}:{port}"
    opener, cj, t = _open(router_ip, timeout, port)

    # 先 GET 首页，拿 lang cookie 初始化
    try:
        with opener.open(base + "/", timeout=t) as r:
            r.read()
    except Exception:
        pass

    for attempt in range(1, retries + 1):
        code, body, has_sid = _try_login(
            opener, cj, router_ip, username, password, t, port
        )

        if has_sid or code == 299:
            # 登录成功后访问首页，获取 -goahead-session- 和 logintoken 等 cookie
            try:
                with opener.open(base + "/", timeout=t) as r:
                    r.read()
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

        # 该账号已有活动会话（脚本上一次异常退出 / 浏览器未退出）：先注销再重试
        if "已经登录" in body:
            print("  [web] 该账号已有活动会话（浏览器或上次脚本未退出），自动注销后重试...")
            try:
                web_logout(router_ip, opener, timeout=t, port=port)
            except Exception:
                pass
            time.sleep(2)

        # 200 + 无 sid + 无"已经登录" + 无 err cookie：最可能是密码错误。
        # 同密码重试只会给光猫失败计数器 +1 加速锁定，直接中止。
        if code == 200 and "已经登录" not in body and not err_info:
            raise RuntimeError(
                f"登录失败（HTTP {code}，未取得 sid，且非会话冲突）："
                f"请核对用户名/密码。响应片段: {body[:150]!r}")

        # 其余情况（会话冲突已注销重试 / 401、403、5xx 等瞬时错误）等待后重试
        if attempt < retries:
            print(f"  [web] 登录未成功（HTTP {code}），{retry_delay}s 后重试 "
                  f"({attempt}/{retries})...")
            print("  提示：光猫只允许一个管理会话，请在浏览器退出登录后再跑脚本")
            time.sleep(retry_delay)
            opener, cj, t = _open(router_ip, timeout, port)
            try:
                with opener.open(base + "/", timeout=t) as r:
                    r.read()
            except Exception:
                pass
            continue

    raise RuntimeError(
        f"登录失败：重试 {retries} 次仍未拿到 session。"
        f"最后 HTTP {code}，响应 {body[:200]}"
    )
