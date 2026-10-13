"""从光猫日志提取 SuPassword 超级密码。

主通道：Web 登录 → 下载日志包(tar) → 本地解压 → 遍历 messages.*.tgz → grep SuPassword
兜底通道：telnet 登录 → 在设备上 grep 日志
"""
import re
import io
import os
import tarfile
import gzip
import tempfile


_SU_PATTERNS = [
    # 日志实际格式形如 "SuPassword=val=XXXX"（GoAhead 打印成 键=val=真实密码），
    # 也兼容 SuPassword=XXXX / SuPassword: XXXX / SuPassword XXXX
    re.compile(rb"SuPassword[=:,\s]+(?:[Vv][Aa][Ll]=)?([^\s,;\"'\r\n]+)"),
    re.compile(rb"supass[=:,\s]+(?:[Vv][Aa][Ll]=)?([^\s,;\"'\r\n]+)"),
]

# 兜底剥离：万一正则未覆盖到的格式，捕获后再清一次开头的 val=
_VAL_PREFIX = re.compile(r"^[Vv][Aa][Ll]=")

# Web 日志下载接口（按实测优先级）
_LOG_DOWNLOAD_URLS = [
    "/command.cgi?logs",
    "/upgrade.cgi?logs",
    "/cgi-bin/logs.cgi",
]

# telnet 兜底：在设备上 grep 的路径
TELNET_LOG_CMDS = [
    "grep -s 'SuPassword' /tmp/tar/logs/messages.0/logs/messages",
    "grep -s 'supass' /tmp/tar/logs/messages.0/logs/messages",
    "grep -s 'SuPassword' /tmp/tar/logs/messages",
    "grep -s 'supass' /tmp/tar/logs/messages",
    "grep -rs 'SuPassword' /tmp/tar/logs/",
    "grep -rs 'supass' /tmp/tar/logs/",
    "grep -s 'SuPassword' /var/log/messages",
    "grep -s 'supass' /var/log/messages",
    "zcat /var/log/messages.0 2>/dev/null | grep 'SuPassword'",
    "zcat /var/log/messages.1 2>/dev/null | grep 'SuPassword'",
]


def _scan_bytes(data, found):
    """对字节流做正则扫描，候选追加到 found。"""
    for pat in _SU_PATTERNS:
        for m in pat.finditer(data):
            val = m.group(1).decode("utf-8", "ignore").strip()
            val = _VAL_PREFIX.sub("", val)  # 再清一次 val= 前缀，双保险
            if val and val not in found:
                found.append(val)


def _extract_tgz(member_bytes):
    """解压 .tgz 字节流，返回 {filename: bytes} 字典。"""
    result = {}
    try:
        gz = gzip.GzipFile(fileobj=io.BytesIO(member_bytes))
        with tarfile.open(fileobj=gz, mode="r|") as tar:
            for m in tar:
                if m.isfile():
                    f = tar.extractfile(m)
                    if f:
                        result[m.name] = f.read()
    except Exception:
        pass
    return result


def fetch_supass_via_web(opener, router_ip, verbose=False):
    """Web 下载日志包 → 本地解压 → grep SuPassword。

    opener 是已登录的 urllib opener（带 session cookie）。
    返回候选列表（可能为空）。
    """
    found = []

    for path in _LOG_DOWNLOAD_URLS:
        url = f"http://{router_ip}{path}"
        if verbose:
            print(f"  [web] 尝试下载 {url}")
        try:
            resp = opener.open(url, timeout=30)
            data = resp.read()
            code = resp.getcode()
        except Exception as e:
            if verbose:
                print(f"  [web] {path} 失败：{e}")
            continue

        if not data or len(data) < 100:
            if verbose:
                print(f"  [web] {path} 响应为空（HTTP {code}）")
            continue

        if verbose:
            print(f"  [web] {path} 下载成功，{len(data)} 字节")

        # 尝试当 tar 解压
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data))
        except Exception:
            if verbose:
                print(f"  [web] 非 tar 格式，跳过")
            continue

        # 遍历 tar 成员，找 .tgz 和 messages 文件
        tmpdir = tempfile.mkdtemp(prefix="supass_")
        try:
            for member in tar.getmembers():
                name = member.name
                # 直接是 messages 文件
                if "messages" in os.path.basename(name) and not name.endswith(".tgz"):
                    f = tar.extractfile(member)
                    if f:
                        _scan_bytes(f.read(), found)
                        if found:
                            break

                # .tgz 压缩日志
                if name.endswith(".tgz"):
                    f = tar.extractfile(member)
                    if f:
                        if verbose:
                            print(f"  [web] 解压 {name}...")
                        inner_files = _extract_tgz(f.read())
                        for iname, idata in inner_files.items():
                            _scan_bytes(idata, found)
                        if found:
                            break

                if found:
                    break
        finally:
            tar.close()
            # 清理临时目录
            try:
                import shutil
                shutil.rmtree(tmpdir)
            except Exception:
                pass

        if found:
            break

    return found


def fetch_supass_via_telnet(router_ip, user="CMCCAdmin", password=None):
    """telnet 登录光猫，在设备上 grep 日志（兜底通道）。

    返回候选列表（可能为空）。
    """
    from .telnet_session import TelnetSession
    sess = TelnetSession(router_ip, timeout=20)
    found = []
    try:
        sess.connect()
        sess.login_user(user, password)
        for cmd in TELNET_LOG_CMDS:
            out = sess.run(cmd, timeout=15)
            if out:
                _scan_bytes(out, found)
            if found:
                break
    finally:
        sess.close()
    return found
