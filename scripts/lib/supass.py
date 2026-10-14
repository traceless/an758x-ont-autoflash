"""从光猫日志提取 SuPassword 超级密码。

主通道：Web 登录 → 下载日志包(tar) → 本地解压 → 遍历 messages.*.tgz → grep SuPassword
兜底通道：telnet 登录 → 在设备上 grep 日志
"""
import re
import io
import os
import tarfile
import gzip
import urllib.request


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


# -------------------- TF/MF（电信/移动）SuPassword：AES 密文解密 --------------------
# TF/MF 的日志里没有明文 SuPassword，需从 dumpdatamodel.cgi 抓密文再用 AES-256-CBC 解密。
# key/IV 与解密方式来自恩山帖 8440823（XG-040G-TF），MF 真机实测同份密钥可用。
_ROUTER_AES_KEY = bytes.fromhex(
    "3D A3 73 D7 DC 82 2E 2A 47 0D EC 37 89 6E 80 D7"
    " 2C 49 B3 16 29 DD C9 97 35 4B 84 03 91 77 9E A4")
_ROUTER_AES_IV = bytes.fromhex("D0 E6 DC CD A7 4A 00 DF 76 0F C0 85 11 CB 05 EA")

# dumpdatamodel 中形如 <SuPassword rw="RW" v="<base64密文>" ealgo="ab">
_DUMP_SU_PATTERNS = [
    re.compile(rb'<SuPassword\b[^>]*\bv="([^"]+)"'),
    re.compile(rb'SuPassword[^>]{0,64}?\bv="([^"]+)"'),
]


def _decrypt_router_supassword(b64_cipher):
    """base64 密文 → AES-256-CBC 解密 → 明文（去 PKCS#7）。失败返回 None。"""
    import base64
    from .aes import cbc_decrypt, pkcs7_unpad
    try:
        raw = base64.b64decode(b64_cipher + "=" * (-len(b64_cipher) % 4))
    except Exception:
        return None
    if not raw or len(raw) % 16 != 0:
        return None
    try:
        pt = pkcs7_unpad(cbc_decrypt(_ROUTER_AES_KEY, _ROUTER_AES_IV, raw))
    except Exception:
        return None
    val = pt.decode("utf-8", "ignore").strip()
    return val or None


def fetch_supass_via_dumpdatamodel(opener, router_ip, profile, verbose=False):
    """TF：dumpdatamodel.cgi 抓数据模型 → 提取 SuPassword 密文 → AES 解密。

    实测两步机制（真机 XG-040G-TF 验证）：
      1. GET  :{mgmt}/dumpdatamodel.cgi        → 页面 HTML 里下发 csrf_token
      2. POST :{mgmt}/dumpdatamodel.cgi?data   → body=csrf_token=<token>，
        需 XHR 头 + 同源 Referer，返回约 450KB 的数据模型文本
    SuPassword 形如 <SuPassword rw="RW" v="<base64密文>" ealgo="ab">。
    返回明文候选列表（可能为空）。
    """
    import re as _re
    mgmt = int(profile.get("mgmt_port", 80))  # 缺省与 telnet_open 一致
    base = f"http://{router_ip}:{mgmt}"

    # 1. 取页面拿 csrf_token
    page_url = f"{base}/dumpdatamodel.cgi"
    if verbose:
        print(f"  [web] GET {page_url} 获取 csrf_token ...")
    try:
        with opener.open(page_url, timeout=15) as resp:
            page = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        if verbose:
            print(f"  [web] {page_url} 失败：{e}")
        return []
    m = _re.search(r'csrf_token"[^>]*value="([^"]+)"', page)  # 属性顺序无关
    if not m:
        if verbose:
            print("  [web] 页面未找到 csrf_token（可能未登录或页面结构变化）")
        return []
    token = m.group(1)
    if verbose:
        print(f"  [web] csrf_token={token}")

    # 2. POST ?data 拉数据模型（带 token 与同源 Referer）
    data_url = f"{page_url}?data"
    req = urllib.request.Request(
        data_url,
        data=urllib.parse.urlencode({"csrf_token": token}).encode(),
        method="POST")  # urlencode：token 含 +/= 时原样拼接会被服务端误解码
    req.add_header("X-Requested-With", "XMLHttpRequest")
    req.add_header("Referer", page_url)
    req.add_header("Content-Type",
                   "application/x-www-form-urlencoded; charset=UTF-8")
    if verbose:
        print(f"  [web] POST {data_url} 下载数据模型 ...")
    try:
        with opener.open(req, timeout=60) as resp:
            data = resp.read()
    except Exception as e:
        if verbose:
            print(f"  [web] {data_url} 失败：{e}")
        return []
    if not data:
        if verbose:
            print("  [web] 数据模型为空")
        return []
    if verbose:
        print(f"  [web] 数据模型 {len(data)} 字节，提取 SuPassword 密文...")

    # 3. 提取密文并解密
    #    两条正则（主匹配 + 兜底）可能同时命中同一个 SuPassword 元素，
    #    故按密文去重，避免同一段密文重复解密/重复打印日志。
    found = []
    seen_cipher = set()
    for pat in _DUMP_SU_PATTERNS:
        for mm in pat.finditer(data):
            c = mm.group(1).decode("ascii", "ignore").strip()
            if not c or c in seen_cipher:
                continue
            seen_cipher.add(c)
            pt = _decrypt_router_supassword(c)
            if verbose:
                print(f"  [web] 密文 v={c} -> {'解密成功' if pt else '解密失败'}")
            if pt and pt not in found:
                found.append(pt)
    return found


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

        # 遍历 tar 成员，找 .tgz 和 messages 文件（全程内存解压，无临时目录）
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
