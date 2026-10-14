#!/usr/bin/env python3
"""XG-040G-MD 光猫免拆刷机助手。

跨平台（macOS / Linux / Windows），纯 Python 标准库实现，零 pip 依赖。

【推荐用法】直接运行，进入 1-6 交互主菜单：
  python3 scripts/flash.py
    1. 检查环境
    2. 开启 telnet
    3. 查询 SuPassword 超密
    4. 执行备份（自动内联启动 HTTP 服务，无需另开终端）
    5. 刷引导（自动先 run-stock2ubi 拉起 :3333，再 web-flash）
    6. U-Boot 恢复页刷机（192.168.0.1，进入后再选 1-5 步）

【高级用法】带子命令可单独执行任一步（脚本化/排障用）：
  check / net-help / enable-telnet / fetch-supass / login
  httpd / tftpd / backup / run-stock2ubi
  web-backup / web-flash / uboot-flash

已刷 PonWrt 重刷/换固件：进 U-Boot 恢复模式 → 主菜单选 6。
"""
import argparse
import atexit
import os
import re
import sys
import subprocess
import threading

# 让 lib 可被 import
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# 默认配置
DEFAULT_ROUTER_IP = "192.168.1.1"
DEFAULT_TFTP_IP = "192.168.1.254"
DEFAULT_TFTP_PORT = 69
DEFAULT_WEB_PORT = 3333
DEFAULT_UBOOT_HOST = "192.168.0.1"
DEFAULT_UBOOT_TIMEOUT = 20
DEFAULT_HTTP_IP = "192.168.1.254"
DEFAULT_HTTP_PORT = 7123

# 机型 profile 统一在 lib/models.py 声明（凭据默认值、固件目录、端口、各步骤方式）。
# Web 账号不写死在代码：从 .env 的 {MODEL}_WEB_USER / {MODEL}_WEB_PASS 读取。
# 固件放机型对应目录（如 uboot-md/），文件名含 preloader/fip/.itb 关键字即可被识别。
from lib.models import DEFAULT_MODEL, DEVICE_MODELS, get_profile  # noqa: E402

# 项目目录
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
BACKUP_DIR = os.path.join(PROJECT_DIR, "backup")
TOOLS_DIR = os.path.join(PROJECT_DIR, "tools")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

# mtd 分区表（index, 备份文件名）—— 仅作 /proc/mtd 读取失败时的 MD 兜底。
# TF 分区名不同（mtd11~16）且多 mtd17/18，实际以设备 /proc/mtd 动态解析为准。
MTD_PARTITIONS = [
    (0, "mtd0-bootloader.bin"),
    (1, "mtd1-romfile.bin"),
    (2, "mtd2-kernel.bin"),
    (3, "mtd3-rootfs.bin"),
    (4, "mtd4-kernel_slave.bin"),
    (5, "mtd5-rootfs_slave.bin"),
    (6, "mtd6-bosa.bin"),  # 光模块校准，务必单独多存
    (7, "mtd7-ri.bin"),
    (8, "mtd8-flag.bin"),
    (9, "mtd9-flagback.bin"),
    (10, "mtd10-config.bin"),
    (11, "mtd11-data.bin"),
    (12, "mtd12-oopsfs.bin"),
    (13, "mtd13-log.bin"),
    (14, "mtd14-nsb_master.bin"),
    (15, "mtd15-nsb_slave.bin"),
    (16, "mtd16-all_flash.bin"),
]

# Windows ping 参数不同
IS_WINDOWS = os.name == "nt"


# -------------------- 公共小工具 --------------------

def write_env(key, value):
    """更新 .env 里某个键（保留其它键）。"""
    lines = []
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    continue
                lines.append(line.rstrip("\n"))
    lines.append(f"{key}={value}")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_env(key, default=None):
    if not os.path.isfile(ENV_FILE):
        return default
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return default


def ping_ok(ip, count=2):
    """跨平台 ping 自检。"""
    flag = "-n" if IS_WINDOWS else "-c"
    wait = "-w" if IS_WINDOWS else "-W"
    cmd = ["ping", flag, str(count), wait, "1", ip]
    ret = subprocess.run(cmd, capture_output=True)
    return ret.returncode == 0


def list_local_ips():
    """枚举本机已绑定的 IPv4（排除环回），返回 [(ip, iface), ...]，去重保序。

    跨平台：Windows 解析 ipconfig；Linux 优先 `ip -o addr`，
    其余（含 macOS）回退 ifconfig。
    """
    result, seen = [], set()

    def add(ip, iface):
        if ip and not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            result.append((ip, iface))

    try:
        if IS_WINDOWS:
            r = subprocess.run(["ipconfig"], capture_output=True, text=True,
                               errors="ignore", timeout=10)
            iface = "以太网"
            for line in r.stdout.splitlines():
                if line.strip() and not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
                    iface = line.strip().rstrip(":")
                if ("IPv4" in line or "IP Address" in line):
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                    if m:
                        add(m.group(1), iface)
        else:
            # Linux 有 ip 命令；macOS 无（FileNotFoundError），需回退 ifconfig
            try:
                r = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True,
                                   text=True, errors="ignore", timeout=10)
            except FileNotFoundError:
                r = None
            if r is not None and r.returncode == 0 and r.stdout.strip():
                # 形如: 2: en0    inet 192.168.1.16/24 brd ... scope global
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[2] == "inet":
                        add(parts[3].split("/")[0], parts[1])
            else:
                r = subprocess.run(["ifconfig"], capture_output=True, text=True,
                                   errors="ignore", timeout=10)
                iface = "?"
                for line in r.stdout.splitlines():
                    m = re.match(r"^([A-Za-z0-9_]+):", line)
                    if m:
                        iface = m.group(1)
                    m = re.search(r"inet (\d{1,3}(?:\.\d{1,3}){3})", line)
                    if m:
                        add(m.group(1), iface)
    except Exception:
        pass
    return result


def same_subnet(ip1, ip2, prefix=24):
    """两个 IP 是否在同一 /24 网段。"""
    import ipaddress
    try:
        net = ipaddress.ip_network(f"{ip2}/{prefix}", strict=False)
        return ipaddress.ip_address(ip1) in net
    except ValueError:
        return False


def resolve_http_ip(args):
    """决定光猫访问本机用的 HTTP IP：
    命令行 --http-ip > 检查环境时选择并存入 .env 的 HTTP_IP > 默认 .254。
    """
    explicit = getattr(args, "http_ip", None)
    if explicit:
        return explicit
    return read_env("HTTP_IP") or DEFAULT_HTTP_IP


def pick_http_ip(router_ip):
    """检查环境时列出本机已绑定 IP 让用户选，返回选定 IP。

    回车使用默认 192.168.1.254；输 m 可手动填写。非交互（管道）直接用默认。
    """
    ips = list_local_ips()
    bound = {ip for ip, _ in ips}
    print("\n=== 5. 本机 HTTP 服务 IP（光猫通过它访问电脑下载/上传文件）===")
    if not ips:
        print(f"  [—] 未枚举到本机网卡 IP，使用默认 {DEFAULT_HTTP_IP}")
        return DEFAULT_HTTP_IP
    for i, (ip, iface) in enumerate(ips, 1):
        tag = " ←与光猫同网段" if same_subnet(ip, router_ip) else ""
        star = "（默认）" if ip == DEFAULT_HTTP_IP else ""
        print(f"  [{i}] {ip:<16} {iface}{star}{tag}")
    print(f"  回车使用默认 {DEFAULT_HTTP_IP}；输 m 手动输入其他 IP")
    try:
        raw = input("请选择: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(f"\n  使用默认 {DEFAULT_HTTP_IP}")
        return DEFAULT_HTTP_IP
    if raw == "":
        return DEFAULT_HTTP_IP
    if raw == "m":
        try:
            manual = input(f"手动输入 IP [{DEFAULT_HTTP_IP}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return DEFAULT_HTTP_IP
        manual = manual or DEFAULT_HTTP_IP
        if manual not in bound:
            print(f"  [WARN] {manual} 不在本机已绑定 IP 列表中，请确认网卡已配置该地址")
        return manual
    if raw.isdigit() and 1 <= int(raw) <= len(ips):
        return ips[int(raw) - 1][0]
    print(f"  无效选择，使用默认 {DEFAULT_HTTP_IP}")
    return DEFAULT_HTTP_IP



def ask(prompt, default=None, secret=False):
    """交互式输入。secret 时不回显。"""
    if default is not None:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    if secret:
        import getpass
        return getpass.getpass(prompt) or default
    val = input(prompt).strip()
    return val or default


def normalize_su_pass(v):
    """剥离 SuPassword 提取时可能误带的 val= 前缀（日志格式 键=val=密码）。"""
    if not v:
        return v
    v = v.strip()
    return re.sub(r"^[Vv][Aa][Ll]=", "", v)


# telnet 用户名/密码：仅在当前主菜单会话（同一进程）内存缓存；
# 只在 telnet 登录成功后写入（remember_telnet），输错的密码不会被复用；
# 不写入 .env，脚本退出即失效（符合每台设备贴纸密码不同、不持久化的要求）。
_SESSION_TELNET = {}


def remember_telnet(user, user_pass):
    """telnet 登录成功后缓存凭据，本会话后续步骤免重复输入。"""
    if user:
        _SESSION_TELNET["user"] = user
    if user_pass:
        _SESSION_TELNET["user_pass"] = user_pass


def forget_telnet():
    """登录失败后清除缓存，下次重新询问（避免错密码被反复使用）。"""
    _SESSION_TELNET.pop("user", None)
    _SESSION_TELNET.pop("user_pass", None)


def get_credentials(args):
    """从 args / 会话缓存 / 交互获取 user / user_pass / su_pass。

    telnet 用户名和密码：命令行参数 > 当前会话已验证的缓存 > 交互输入；
    缓存仅在登录成功后写入，输错会重新询问。
    su 超密从 .env 复用（fetch-supass 提取）。
    TF 机型（telnet_login=root）无贴纸账号：直接以 root + 超密登录。
    """
    model = resolve_model(args)
    prof = get_profile(model)
    su_pass = normalize_su_pass(
        getattr(args, "su_pass", None) or read_env("SU_PASS")
        or ask("SU 超级密码（fetch-supass 提取）", secret=True))
    write_env("SU_PASS", su_pass)  # 回写清洗后的值，旧的 val= 脏值自愈

    if prof.get("telnet_login") == "root":
        return "root", su_pass, su_pass  # TF：root + 超密直登

    user = (getattr(args, "user", None)
            or _SESSION_TELNET.get("user")
            or ask("telnet 用户名（光猫背后贴纸，user 或 useradmin）"))
    user_pass = (getattr(args, "user_pass", None)
                 or _SESSION_TELNET.get("user_pass")
                 or ask("telnet 密码（光猫背后'默认终端配置密码'）", secret=True))
    return user, user_pass, su_pass


def resolve_model(args=None):
    """解析当前机型：--model > .env DEVICE_MODEL > 默认 MD。"""
    model = ((getattr(args, "model", None) if args else None)
             or read_env("DEVICE_MODEL") or DEFAULT_MODEL)
    model = str(model).upper()
    if model not in DEVICE_MODELS:
        print(f"[WARN] 未知机型 {model}，回退到 {DEFAULT_MODEL}")
        model = DEFAULT_MODEL
    return model


def model_uboot_dir(model=None):
    """机型固件目录（如 uboot-md/），来自 DEVICE_MODELS profile。"""
    prof = DEVICE_MODELS.get(model or DEFAULT_MODEL, {})
    return os.path.join(
        PROJECT_DIR, prof.get("uboot_dir", (model or DEFAULT_MODEL).lower() + "-uboot"))


def find_bl2_fip(model=None):
    """在机型固件目录用通配符找 preloader / fip 文件，返回 (bl2, fip)。

    文件名不写死：文件名含 preloader / fip（或 bl2 / bl31）关键字即可识别，
    便于直接放入目录使用。
    """
    import glob
    d = model_uboot_dir(model)
    if not os.path.isdir(d):
        return None, None

    def _first(*patterns):
        for pat in patterns:
            m = sorted(glob.glob(os.path.join(d, pat)))
            if m:
                return m[0]
        return None

    bl2 = _first("*preloader*.bin", "*bl2*.bin")
    fip = _first("*fip*.fip", "*bl31*.fip", "*fip*.bin")
    return bl2, fip


def get_web_credentials(args):
    """按机型获取 Web 超管凭据，返回 (model, user, password)。

    机型：命令行 --model > .env DEVICE_MODEL > 默认 MD。
    账号：命令行 --web-user > .env {MODEL}_WEB_USER
          > 交互输入（不写死在代码，输入后写入 .env）。
    密码：命令行 --web-pass > .env {MODEL}_WEB_PASS > 旧 WEB_PASS（MD 迁移）
          > 交互输入（回车用该机型默认密码，并提醒机型间默认密码不同）。
    """
    model = resolve_model(args)
    prof = DEVICE_MODELS[model]

    user = getattr(args, "web_user", None) or read_env(f"{model}_WEB_USER")
    if not user:
        print(f"\n[web] 当前机型：{prof['name']}")
        du = prof.get("default_user")
        user = ask(
            f"Web 超管用户名（回车默认 {du}）" if du
            else "Web 超管用户名", du)
        while not user:
            user = ask("用户名不能为空，请重新输入")
    saved = read_env(f"{model}_WEB_PASS")
    if not saved and model == DEFAULT_MODEL:
        saved = read_env("WEB_PASS")  # 兼容旧版本 .env 的单一 WEB_PASS
    password = getattr(args, "web_pass", None) or saved
    if not password:
        print(f"\n[web] 当前机型：{prof['name']}   账号：{user}")
        print("  注意：移动定制光猫不同机型默认密码不同；若被运维改过请输入实际密码。")
        password = ask(
            f"{user} 的 Web 密码（回车用 {model} 默认 {prof['default_pass']}）",
            prof["default_pass"], secret=True)
    # 注意：此处不落盘。密码只在 web_login 成功后经 save_web_credentials 写入
    # .env，输错的密码不会被持久化，也绝不会被后续步骤复用。
    return model, user, password


def save_web_credentials(model, user, password):
    """Web 登录成功后把凭据写入 .env（输错的密码绝不持久化）。"""
    write_env("DEVICE_MODEL", model)
    write_env(f"{model}_WEB_USER", user)
    write_env(f"{model}_WEB_PASS", password)


# -------------------- 子命令 --------------------

def cmd_check(args):
    print("=== 1. 依赖检查 ===")
    try:
        from lib import _telnetlib
        print(f"  [OK] telnet -> lib/_telnetlib.py（vendored 官方实现，无版本依赖）")
    except ImportError as e:
        print(f"  [FAIL] telnet 模块不可用: {e}")
    try:
        import urllib.request, json, tarfile
        print("  [OK] urllib / json / tarfile")
    except ImportError as e:
        print(f"  [FAIL] {e}")

    model = resolve_model(args)
    uboot_dir = model_uboot_dir(model)
    print(f"\n当前机型: {DEVICE_MODELS[model]['name']}（.env DEVICE_MODEL={model}）")

    print("\n=== 2. 资源目录 ===")
    for name, path in [("backup", BACKUP_DIR),
                       (f"{os.path.basename(uboot_dir)} 固件", uboot_dir),
                       ("tools", TOOLS_DIR)]:
        ok = os.path.isdir(path)
        print(f"  [{'OK' if ok else 'MISS'}] {name}: {path}")

    print("\n=== 3. 关键文件 ===")
    bl2, fip = find_bl2_fip(model)
    checks = [
        ("preloader.bin", bl2),
        ("bl31-u-boot.fip", fip),
        ("mtd6-bosa.bin(校准)", os.path.join(BACKUP_DIR, "mtd6-bosa.bin")),
        ("an758x-stock2ubi", os.path.join(TOOLS_DIR, "an758x-stock2ubi")),
    ]
    fw = _find_itb(model)
    checks.append(("sysupgrade.itb", fw))
    for name, path in checks:
        if path and os.path.isfile(path):
            print(f"  [OK] {name}: {path} ({os.path.getsize(path)} 字节)")
        else:
            print(f"  [MISS] {name}: {path}")

    print(f"\n=== 4. 路由器连通性 {args.router} ===")
    if ping_ok(args.router):
        print(f"  [OK] {args.router} 可达")
    else:
        print(f"  [FAIL] {args.router} 不可达")
        print("    请先：1) 网线插光猫千兆口  2) 给网卡设 192.168.1.x 的 IP")
        print("          运行 python3 flash.py net-help 查看设 IP 命令")

    # 命令行显式指定 --http-ip 时直接用；否则列出已绑定 IP 让用户选并记住
    if getattr(args, "http_ip", None):
        http_ip = args.http_ip
        print(f"\n=== 5. 本机服务 IP {http_ip}（命令行 --http-ip 指定）===")
    else:
        http_ip = pick_http_ip(args.router)
        args.http_ip = http_ip
        write_env("HTTP_IP", http_ip)  # 记住选择，后续 backup/run-stock2ubi 复用
    if ping_ok(http_ip):
        print(f"  [OK] 本机已绑定 {http_ip}")
    else:
        print(f"  [FAIL] {http_ip} 不可达，请重新选择或用 net-help 配置网卡 IP")
    import socket as _sock
    _s = _sock.socket()
    _s.settimeout(1)
    try:
        _s.connect(("127.0.0.1", args.http_port))
        _s.close()
        print(f"  [OK] HTTP 服务在 :{args.http_port} 监听中（备份/刷引导时也会自动内联启动）")
    except Exception:
        print(f"  [—] HTTP 服务当前未启动（执行备份/刷引导时会自动内联启动，无需手动开）")

    print("\n=== 6. :3333 Web 服务（原厂阶段，需先在光猫跑 an758x-stock2ubi）===")
    if ping_ok(args.router):
        import urllib.request
        try:
            urllib.request.urlopen(
                f"http://{args.router}:{DEFAULT_WEB_PORT}/", timeout=2
            )
            print(f"  [OK] :3333 已就绪，可 web-backup / web-flash")
        except Exception as e:
            print(f"  [—] :3333 未开（原厂阶段才需要，已刷 PonWrt 可忽略）")


def cmd_net_help(args):
    print("""三平台给有线网卡设多 IP 的命令清单
（刷机需要 192.168.1.16 访问系统 + 192.168.0.16 访问 U-Boot 恢复页；
  若刷原厂机走 backup/run-stock2ubi，还需再加 192.168.1.254 作 HTTP 服务器地址）

【macOS】
  # 查有线网卡服务名
  networksetup -listallnetworkservices
  # 设主 IP（假设服务名 "Ethernet"，接口 en0）
  sudo networksetup -setmanual IP "Ethernet" 192.168.1.16 255.255.255.0
  # 加额外 IP（alias）
  sudo ifconfig en0 alias 192.168.0.16 255.255.255.0
  sudo ifconfig en0 alias 192.168.1.254 255.255.255.0   # 仅原厂阶段需要
  # 删除：sudo ifconfig en0 delete 192.168.0.16

【Linux】（接口名 eth0 按实际）
  sudo ip addr add 192.168.1.16/24 dev eth0
  sudo ip addr add 192.168.0.16/24 dev eth0
  sudo ip addr add 192.168.1.254/24 dev eth0            # 仅原厂阶段需要
  # 删除：sudo ip addr del 192.168.0.16/24 dev eth0

【Windows】（管理员 PowerShell，"以太网" 按实际）
  netsh interface ip add address "以太网" 192.168.1.16 255.255.255.0
  netsh interface ip add address "以太网" 192.168.0.16 255.255.255.0
  netsh interface ip add address "以太网" 192.168.1.254 255.255.255.0  # 仅原厂阶段需要
  # 删除：netsh interface ip delete address "以太网" 192.168.0.16

刷机完成后请删除额外 IP，避免影响日常网络。
""")


def cmd_enable_telnet(args):
    from lib.web_login import web_login, web_logout
    from lib.telnet_open import open_telnet
    write_env("ROUTER_IP", args.router)

    model, user, password = get_web_credentials(args)
    prof = get_profile(model)
    port = int(prof.get("web_port", 80))

    print(f"\n[web] 登录 http://{args.router}:{port}/ （{prof['name']} / {user}）...")
    try:
        opener, cj = web_login(args.router, user, password, port=port)
    except Exception as e:
        print(f"[FAIL] Web 登录失败：{e}")
        print("  本次输入的密码未被保存，重跑会重新询问。")
        print(f"  若 .env 里存的是旧密码导致反复失败：删除 {model}_WEB_PASS 行，"
              "或用 --web-pass 覆盖。")
        print("  检查：账号密码是否正确；光猫是否恢复出厂"
              "（MD 长按 reset 12 秒；TF 拔光纤+长按 reset 到指示灯闪烁）")
        return 1
    save_web_credentials(model, user, password)  # 登录成功才持久化

    print("[web] 登录成功，正在开启 telnet 服务...")
    ok, msg = open_telnet(prof, args.router, opener=opener)
    web_logout(args.router, opener, port=port)
    if ok:
        print(f"[OK] {msg}")
        return 0
    print(f"[FAIL] {msg}")
    mgmt = prof.get("mgmt_port", port)
    print(f"  请手动访问 http://{args.router}:{mgmt}/system.cgi?telnet 点击'打开'")
    return 1


def cmd_fetch_supass(args):
    from lib.web_login import web_login, web_logout
    from lib.supass import fetch_supass_via_web, fetch_supass_via_dumpdatamodel
    write_env("ROUTER_IP", args.router)

    # 凭据（按机型）
    model, web_user, web_pass = get_web_credentials(args)
    prof = get_profile(model)
    port = int(prof.get("web_port", 80))
    mode = prof.get("supass_mode", "log_plaintext")

    # 1. Web 登录
    print(f"\n[web] 登录 http://{args.router}:{port}/ "
          f"（{prof['name']} / {web_user}）...")
    try:
        opener, cj = web_login(args.router, web_user, web_pass, port=port)
        print("[web] 登录成功")
    except Exception as e:
        print(f"[FAIL] Web 登录失败：{e}")
        print("  本次输入的密码未被保存，重跑会重新询问。")
        print(f"  若 .env 里存的是旧密码导致反复失败：删除 {model}_WEB_PASS 行，"
              "或用 --web-pass 覆盖。")
        print("  检查：账号密码是否正确；光猫是否恢复出厂"
              "（MD 长按 reset 12 秒；TF 拔光纤+长按 reset 到指示灯闪烁）")
        return 1
    save_web_credentials(model, web_user, web_pass)  # 登录成功才持久化

    # 2. 按机型取 SuPassword
    if mode == "dumpdatamodel_aes":
        print("\n[web] 从 dumpdatamodel.cgi 抓取 SuPassword 密文并 AES 解密...")
        found = fetch_supass_via_dumpdatamodel(opener, args.router, prof, verbose=True)
    else:
        print("\n[web] 下载日志包并提取 SuPassword...")
        found = fetch_supass_via_web(opener, args.router, verbose=True)

    # 退出 Web 会话，避免遗留
    web_logout(args.router, opener, port=port)

    if not found:
        if mode == "dumpdatamodel_aes":
            mgmt = prof.get("mgmt_port", port)
            print("\n[FAIL] 未从 dumpdatamodel 解出 SuPassword。")
            print(f"  可浏览器登录后访问 http://{args.router}:{mgmt}/dumpdatamodel.cgi，"
                  "搜 SuPassword 的 v= 值核对")
        else:
            print("\n[FAIL] 日志中没找到 SuPassword。")
            print("  可浏览器登录 Web → /upgrade.cgi 手动下载日志，搜 SuPassword / supass")
        return 1

    # 再统一清洗一次（telnet 兜底通道返回的是原始文本，可能带 val=）
    found = [normalize_su_pass(p) for p in found]
    found = [p for p in found if p]
    print("\n找到 SuPassword 候选：")
    for i, p in enumerate(found):
        print(f"  [{i}] {p}")
    write_env("SU_PASS", found[0])
    print(f"\n已将首个候选写入 {ENV_FILE}：")
    print(f"  SU_PASS={found[0]}")
    print("  若登录失败，可换其它候选重试")
    return 0


def cmd_login(args):
    from lib.telnet_session import TelnetSession
    prof = get_profile(resolve_model(args))
    user, user_pass, su_pass = get_credentials(args)
    sess = TelnetSession(args.router, timeout=20)
    try:
        sess.connect_login(prof.get("telnet_login", "user_su"), user, user_pass, su_pass)
        remember_telnet(user, user_pass)  # 验证通过才缓存
        print("\n[OK] 登录成功，已进入 root")
        out = sess.run("uname -a; echo '---'; cat /proc/mtd", timeout=20)
        print("\n=== 设备信息 ===")
        print(out.decode("utf-8", "ignore"))
    finally:
        sess.close()


def cmd_tftpd(args):
    from tftp_server import TftpServer
    root = os.path.abspath(args.root or PROJECT_DIR)
    if not os.path.isdir(root):
        print(f"[FAIL] TFTP 根目录不存在: {root}")
        return 1
    os.makedirs(BACKUP_DIR, exist_ok=True)
    srv = TftpServer(root, "0.0.0.0", args.port)
    try:
        srv.serve_forever()
    except PermissionError:
        print("错误：端口 69 需要特权。")
        print("  macOS/Linux:  sudo python3 flash.py tftpd")
        print("  Windows:     以管理员身份运行")
        return 1
    except KeyboardInterrupt:
        print("\n[tftp] 已停止")
    return 0


def cmd_httpd(args):
    from http_server import make_server
    root = os.path.abspath(args.root or PROJECT_DIR)
    if not os.path.isdir(root):
        print(f"[FAIL] HTTP 根目录不存在: {root}")
        return 1
    os.makedirs(BACKUP_DIR, exist_ok=True)
    port = args.http_port
    server = make_server(port, root)
    print(f"[http] listening on 0.0.0.0:{port}  root={root}")
    print(f"  不需要 sudo（端口 {port}）")
    print(f"  下载: curl -o /tmp/xxx http://<本机IP>:{port}/tools/xxx")
    print(f"  上传: dd if=/dev/mtdN | curl -T - http://<本机IP>:{port}/backup/name.bin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\n[http] 已停止")
    return 0


_httpd_singleton = None
_httpd_addr = None  # (access_host, port)，退出时向它 POST /__shutdown
_httpd_lock = threading.Lock()


def shutdown_httpd():
    """脚本退出时关闭 HTTP 服务：向服务自身 POST /__shutdown。

    进程内内联服务和复用的外部独立 httpd 统一走这条路径，
    外部独立进程收到请求后 serve_forever 返回并退出，不留残留进程。
    """
    global _httpd_singleton, _httpd_addr
    with _httpd_lock:
        addr = _httpd_addr
        _httpd_addr = None
        _httpd_singleton = None
    if not addr:
        return
    host, port = addr
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/__shutdown", data=b"", method="POST"
        )
        urllib.request.urlopen(req, timeout=2).read()
        print(f"[http] 已通知 {host}:{port} 关闭服务")
    except Exception:
        pass  # 服务可能已退出，忽略


atexit.register(shutdown_httpd)


def ensure_httpd(port=None, root=None, host=None):
    """在后台守护线程内联启动 HTTP 服务器（进程内单例）。

    主菜单的「备份」「web-flash」自动调用，免去另开终端手动跑 httpd。
    启动前先探测设备实际访问的 host:port，已有 HTTP 服务则直接复用
    （例如另开终端跑的 httpd 或上轮残留的同项目服务），不再因端口占用崩溃。
    注意：只监听 127.0.0.1 的程序（如部分编辑器扩展）光猫访问不到，不会被误判。
    脚本退出时统一 POST /__shutdown 关闭（含复用的外部独立服务）。
    """
    global _httpd_singleton, _httpd_addr
    from http_server import make_server
    port = port or DEFAULT_HTTP_PORT
    root = os.path.abspath(root or PROJECT_DIR)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with _httpd_lock:
        if _httpd_singleton is not None:
            return _httpd_singleton
        # 1) 设备访问地址上已有 HTTP 服务 → 直接复用
        if host and http_url_ok(f"http://{host}:{port}/", timeout=1.5):
            print(f"[http] 检测到 {host}:{port} 已有 HTTP 服务，直接复用（无需重复启动）")
            _httpd_singleton = "external"
            _httpd_addr = (host, port)
            return _httpd_singleton
        # 2) 绑定：优先设备访问 IP（避开仅占 127.0.0.1 的程序），失败回退 0.0.0.0
        candidates = [host, "0.0.0.0"] if host else ["0.0.0.0"]
        last_err = None
        for bind_host in candidates:
            try:
                srv = make_server(port, root, host=bind_host)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                _httpd_singleton = srv
                # 绑具体 IP 时用该 IP 关；绑 0.0.0.0 时用回环关
                access = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
                _httpd_addr = (access, port)
                print(f"[http] 内置服务已启动 {bind_host}:{port}（root={root}）")
                return _httpd_singleton
            except OSError as e:
                last_err = e
                continue
        raise RuntimeError(
            f"HTTP 端口 {port} 无法绑定（{last_err}）。\n"
            f"  排查：lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
            f"  若是其他程序占用，可用 --http-port 换端口后重跑。"
        )
    return _httpd_singleton


def http_url_ok(url, timeout=2):
    """探测某 HTTP 地址是否可达。"""
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def stock2ubi_ready(router, port=DEFAULT_WEB_PORT):
    """探测光猫上 an758x-stock2ubi 的 :3333 服务是否就绪。"""
    return http_url_ok(f"http://{router}:{port}/", timeout=2)


def wait_stock2ubi(router, port=DEFAULT_WEB_PORT, seconds=20):
    """轮询等待 :3333 就绪。"""
    import time
    for i in range(max(1, seconds // 2)):
        if stock2ubi_ready(router, port):
            return True
        time.sleep(2)
    return False


def uboot_recovery_up(host=DEFAULT_UBOOT_HOST, port=80, timeout=2):
    """探测 U-Boot 恢复页是否就绪：能建立 TCP 连接即代表其 HTTP 服务已起。

    用 TCP 探活而非 GET 具体路径，避免恢复页根路径返回非 200 造成误判。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_uboot_recovery(host=DEFAULT_UBOOT_HOST, seconds=DEFAULT_UBOOT_TIMEOUT):
    """web-flash 后探测设备是否重启进入 U-Boot 恢复页（192.168.0.1:80）。

    返回 True/False。超时不抛异常——刷引导本身已成功，恢复页可能只是起得慢
    或本机未绑 192.168.0.16，给出可操作提示即可。
    """
    import time
    print(f"\n[probe] 探测 U-Boot 恢复页 http://{host}/（超时 {seconds}s，设备正在重启）...")
    start = time.time()
    while time.time() - start < seconds:
        if uboot_recovery_up(host):
            print(f"\r[OK] U-Boot 恢复页 {host} 已就绪"
                  f"（用时 {int(time.time()-start)}s），可进入第 6 步 uboot-flash。")
            return True
        time.sleep(1)
        print(f"\r  等待设备进入恢复模式... {int(time.time()-start)}s/{seconds}s",
              end="", flush=True)
    print(f"\n[WARN] {seconds}s 内未探到 {host}:80。")
    print("  排查/处理：")
    print(f"  1) 本机需已绑定 192.168.0.16（运行 net-help 查看），否则连不到 {host}")
    print("  2) 设备重启进恢复页可能稍慢，再等 10~30s 后浏览器访问 http://192.168.0.1")
    print("  3) 确认页面可打开后，回主菜单选 6 开始刷系统；始终起不来则拆机走 TTL")
    return False


def _chunk_partitions(partitions, size=7):
    """把分区列表切成若干批（单条命令过长可能超出 shell 限制）。"""
    return [partitions[i:i + size] for i in range(0, len(partitions), size)]


def _batch_label(n, items):
    if len(items) == 1:
        return "仅 " + items[0][1]
    return f"第{n}批 mtd{items[0][0]}~mtd{items[-1][0]}（{len(items)} 个）"


def _build_backup_cmds_http(http_ip, http_port, partitions):
    """构造 dd|curl PUT 命令（用 HTTP 替代 TFTP）。文件落到 backup/。

    partitions 为要备份的 [(mtd_index, 文件名), ...]，动态分批。
    """
    def chain(items):
        return " && ".join(
            f"dd if=/dev/mtd{i} | curl -fsS --connect-timeout 5 -T - http://{http_ip}:{http_port}/backup/{name}"
            for i, name in items
        )
    if len(partitions) <= 1:
        return [(_batch_label(1, partitions), chain(partitions))]
    return [(_batch_label(n, batch), chain(batch))
            for n, batch in enumerate(_chunk_partitions(partitions), 1)]


def _build_backup_cmds(tftp_ip, partitions):
    """构造 dd|tftp 长命令（TFTP 兜底）。文件落到 backup/。"""
    def chain(items):
        return " && ".join(
            f"dd if=/dev/mtd{i} | tftp -p -l - -r backup/{name} {tftp_ip}"
            for i, name in items
        )
    if len(partitions) <= 1:
        return [(_batch_label(1, partitions), chain(partitions))]
    return [(_batch_label(n, batch), chain(batch))
            for n, batch in enumerate(_chunk_partitions(partitions), 1)]


def read_mtd_table(sess, retries=1):
    """在已登录的 telnet 会话上读 /proc/mtd，返回 [(index, name, size)]（按 index 升序）。

    size 为分区字节数（/proc/mtd 的 size 列），用于备份后文件大小校验。
    读取为空时重试一次（run 每次会先做 shell 握手，竞态已由握手兜底）。
    """
    for attempt in range(retries + 1):
        out = sess.run("cat /proc/mtd", timeout=15)
        table = []
        for m in re.finditer(r'mtd(\d+):\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"([^"]+)"',
                             out.decode("utf-8", "ignore")):
            table.append((int(m.group(1)), m.group(3).strip(), int(m.group(2), 16)))
        table.sort()
        if table:
            return table
        print(f"[WARN] /proc/mtd 读取为空（第 {attempt + 1} 次），重试...")
    return []


def build_backup_partitions(mtd_table, mode):
    """按备份范围生成 [(mtd_index, 文件名), ...]。

    bosa 模式备份两块关键数据：bosa（光模块校准）+ ri（射频/区域信息，
    同样不可再生）。按名称从 /proc/mtd 定位，缺失时回退惯例 mtd6/mtd7。
    """
    if mode == "bosa":
        parts = []
        for want, fallback in (("bosa", 6), ("ri", 7)):
            hit = [(i, n) for i, n, _ in mtd_table if n == want]
            if hit:
                i, n = hit[0]
            else:
                i, n = fallback, want
            parts.append((i, f"mtd{i}-{n}.bin"))
        return parts
    return [(i, f"mtd{i}-{name}.bin") for i, name, _ in mtd_table]


def _verify_backup_files(partitions, mtd_sizes):
    """备份后按 /proc/mtd 声明大小逐文件校验，杜绝 curl 静默失败仍报成功。

    curl 连接失败/HTTP 400/链路中断会让文件缺失或截断（&& 链中断后续分区
    不再执行）；dd 中途死掉时 curl 仍正常收尾，只有大小对拍能发现截断。
    返回失败列表 [(文件名, 原因), ...]；大小不符的文件加 .bad 后缀防止误用。
    """
    failures = []
    for i, name in partitions:
        path = os.path.join(BACKUP_DIR, name)
        expect = mtd_sizes.get(i)
        if not os.path.isfile(path):
            failures.append((name, "文件缺失（传输链在该分区前已中断）"))
            continue
        actual = os.path.getsize(path)
        if expect is not None and actual != expect:
            bad = path + ".bad"
            os.rename(path, bad)
            failures.append((name,
                             f"大小不符（实际 {actual}B，/proc/mtd 声明 {expect}B），"
                             f"已改名 {os.path.basename(bad)} 防止误用"))
    return failures


def _choose_backup_mode(args):
    """决定备份范围：CLI --mode 优先；否则交互二选一（默认仅 bosa）。

    返回 (mode 字符串, label)。具体分区在登录后按 /proc/mtd 动态解析。
    """
    mode = getattr(args, "backup_mode", None)
    if mode not in ("bosa", "full"):
        print("\n=== 备份范围 ===")
        print("  1. 仅备份关键分区（bosa 校准 + ri，体积小，推荐）")
        print("  2. 全量备份所有分区（按设备 /proc/mtd 实际表，体积大、耗时久）")
        try:
            raw = input("请选择 [1/2]（回车默认 1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n默认选 1（bosa + ri）")
            raw = ""
        mode = "full" if raw == "2" else "bosa"
    label = "仅关键分区（bosa + ri）" if mode == "bosa" else "全量（按 /proc/mtd）"
    return mode, label


def cmd_backup(args):
    from lib.telnet_session import TelnetSession
    # 先选备份范围（仅 bosa / 全量），再要凭据
    mode, scope_label = _choose_backup_mode(args)
    args.http_ip = resolve_http_ip(args)
    login_mode = get_profile(resolve_model(args)).get("telnet_login", "user_su")
    user, user_pass, su_pass = get_credentials(args)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # 内联启动 HTTP 服务（后台线程），无需另开终端
    ensure_httpd(args.http_port, host=args.http_ip)
    sess = TelnetSession(args.router, timeout=20)
    try:
        sess.connect_login(login_mode, user, user_pass, su_pass)
    except Exception:
        forget_telnet()  # 登录失败，清缓存让下次重新询问
        raise
    remember_telnet(user, user_pass)  # 验证通过才缓存
    try:
        # 登录后按 /proc/mtd 动态解析分区（TF 与 MD 分区名/数量不同，勿用硬编码表）
        mtd_table = read_mtd_table(sess)
        if not mtd_table:
            # 硬编码 MD 表在 TF 上文件名全错（mtd11~16 名称不同且少 2 个分区），
            # 宁可中止也不能备错名
            print("[FAIL] /proc/mtd 读取失败（重试后仍为空），为避免备错分区名已中止。"
                  "请重跑本步；若反复失败请先执行 login 排查会话状态")
            return 1
        partitions = build_backup_partitions(mtd_table, mode)
        mtd_sizes = {i: sz for i, _, sz in mtd_table}
        if mode == "bosa":
            print("[OK] 关键分区定位: "
                  + ", ".join(f'mtd{i} "{n}"' for i, n in partitions))
        else:
            print(f"[OK] 设备共 {len(partitions)} 个分区: "
                  + ", ".join(f"mtd{i}={n}" for i, n, _ in mtd_table))
        # 同名旧备份先归档，避免覆盖（mtd6-bosa.bin 校准数据丢了无法找回）
        for _, name in partitions:
            rotated = rotate_existing(os.path.join(BACKUP_DIR, name))
            if rotated:
                print(f"  (旧备份归档 -> {rotated})")
        print(f"备份范围: {scope_label}（共 {len(partitions)} 个分区）")
        print(f"备份文件将落到: {BACKUP_DIR}")
        print(f"光猫访问本机的 HTTP IP: {args.http_ip}:{args.http_port}（可在检查环境里改选）")
        for label, cmd in _build_backup_cmds_http(
                args.http_ip, args.http_port, partitions):
            print(f"\n=== {label} ===")
            print(f"$ {cmd[:120]}{'...' if len(cmd) > 120 else ''}")
            out = sess.run(cmd, timeout=900)
            print(out.decode("utf-8", "ignore"))
        # 备份后完整性校验：curl 失败/链路中断时文件会缺失或截断，
        # 按 /proc/mtd 声明大小逐一对拍，杜绝"传输失败仍报成功"
        print("\n=== 大小校验（对照 /proc/mtd 声明） ===")
        for i, name in partitions:
            path = os.path.join(BACKUP_DIR, name)
            expect = mtd_sizes.get(i)
            if os.path.isfile(path):
                print(f"  [OK] {name} ({os.path.getsize(path)} B)")
            else:
                print(f"  [MISS] {name}（缺失，应为 {expect} B）")
        failures = _verify_backup_files(partitions, mtd_sizes)
        if failures:
            print(f"\n[FAIL] 备份不完整（{len(failures)}/{len(partitions)} 个分区异常），"
                  "以上非 [OK] 文件不可用于恢复：")
            for name, reason in failures:
                print(f"  - {name}: {reason}")
            print("  常见原因：HTTP 服务/网络中断。可直接重跑本步重试。")
            return 1
        print(f"\n[OK] 备份完成并通过大小校验（{scope_label}，"
              f"{len(partitions)}/{len(partitions)} 个文件）。文件在 {BACKUP_DIR}")
        print("  务必把 bosa（校准）和 ri 这两个文件单独多存几处！")
    finally:
        sess.close()
    return 0


def cmd_run_stock2ubi(args):
    """在光猫上下载并后台运行 an758x-stock2ubi，起 :3333。返回 0/1。"""
    from lib.telnet_session import TelnetSession
    tool = os.path.join(TOOLS_DIR, "an758x-stock2ubi")
    if not os.path.isfile(tool):
        print(f"[FAIL] 免拆包不存在: {tool}")
        return 1
    args.http_ip = resolve_http_ip(args)
    # :3333 已在跑则不重复拉起（重复 nohup 会端口冲突）
    if stock2ubi_ready(args.router):
        print(f"[OK] :{DEFAULT_WEB_PORT} 已就绪，跳过拉起 an758x-stock2ubi")
        return 0
    # 内联 HTTP 服务，供光猫 curl 下载
    ensure_httpd(args.http_port, host=args.http_ip)
    expect_size = os.path.getsize(tool)
    login_mode = get_profile(resolve_model(args)).get("telnet_login", "user_su")
    user, user_pass, su_pass = get_credentials(args)
    sess = TelnetSession(args.router, timeout=20)
    try:
        sess.connect_login(login_mode, user, user_pass, su_pass)
    except Exception:
        forget_telnet()  # 登录失败，清缓存让下次重新询问
        raise
    remember_telnet(user, user_pass)  # 验证通过才缓存
    try:
        # 0. 若设备上已有进程在跑（上次脚本超时但进程未死，仍在初始化），
        #    不重复拉起，直接跳到等待 :3333。
        #    注意必须整行锚定匹配：命令回显行里含 "S2U_RUNNING" 字样，
        #    子串匹配会把回显误判成真输出（TF 真机踩坑：误报已有进程、跳过下载）
        out = sess.run("pidof an758x-stock2ubi && echo S2U_RUNNING || echo S2U_NOT_RUNNING", timeout=15)
        if re.search(rb"^S2U_RUNNING\r?$", out, re.M):
            print("[telnet] 设备上已有 an758x-stock2ubi 进程在运行（可能在初始化），直接等待 :3333")
        else:
            # 1. 下载 + 赋可执行权限 + 校验：单条 && 链由设备 shell 保证顺序，
            #    ls -l/wc -c 输出作为执行证据（-rwx 且字节数与本地一致才算成功）。
            dl_cmd = (
                "cd /tmp && rm -f an758x-stock2ubi && "
                f"curl -fsS -o an758x-stock2ubi http://{args.http_ip}:{args.http_port}/tools/an758x-stock2ubi && "
                "chmod +x an758x-stock2ubi && "
                "ls -l an758x-stock2ubi && wc -c an758x-stock2ubi"
            )
            print(f"\n$ {dl_cmd}")
            ok = False
            for attempt in range(1, 3):
                out = sess.run(dl_cmd, timeout=120)
                text = out.decode("utf-8", "ignore")
                print(text)
                if b"-rwx" in out and str(expect_size).encode() in out:
                    ok = True
                    break
                print(f"[WARN] 下载/赋权校验未通过（期望 -rwx 且 {expect_size} 字节），"
                      f"重试 {attempt}/2 ...")
            if not ok:
                print("[FAIL] 免拆包下载或 chmod +x 失败（见上方 ls -l/wc -c 输出），中止")
                return 1
            # 2. 拉起：nohup + stdin 重定向 /dev/null（防止后台进程占用 tty 吃掉后续输入），
            #    输出重定向防止 sess.close() 发 exit 杀掉进程
            run_cmd = "nohup /tmp/an758x-stock2ubi </dev/null >/tmp/stock2ubi.log 2>&1 &"
            print(f"\n$ {run_cmd}")
            sess.run(run_cmd, timeout=10)
    finally:
        sess.close()
    # nohup 进程已脱离会话，轮询 :3333 就绪作为成功证据。
    # 首次启动需 NAND/UBI 初始化，:3333 可能要 30s+ 才打开，给足 90s。
    print(f"\n等待 :{DEFAULT_WEB_PORT} 服务就绪（首次启动需初始化，最长 90s）...")
    if wait_stock2ubi(args.router, seconds=90):
        print(f"[OK] an758x-stock2ubi 已运行，:{DEFAULT_WEB_PORT} 就绪。")
        return 0
    print(f"[FAIL] :{DEFAULT_WEB_PORT} 90s 内未就绪。")
    print("  请 telnet 检查: cat /tmp/stock2ubi.log; pidof an758x-stock2ubi")
    print("  若日志显示 Permission denied：重新执行本步（会自动重新下载赋权）")
    print("  若进程在跑只是慢：稍等后重新执行本步（已就绪会自动跳过拉起）")
    return 1


def rotate_existing(path):
    """若 path 已存在，先移入 archive/ 加时间戳，避免覆盖。返回归档路径或 None。"""
    if not os.path.isfile(path):
        return None
    import shutil
    import time
    arch_dir = os.path.join(os.path.dirname(path), "archive")
    os.makedirs(arch_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    dst = os.path.join(arch_dir, f"{base}.{ts}.bak")
    shutil.move(path, dst)
    return dst


def cmd_web_backup(args):
    from lib.http_flash import backup_partition
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ok, fail = 0, 0
    for index, name in MTD_PARTITIONS:
        out = os.path.join(BACKUP_DIR, name)
        rotated = rotate_existing(out)
        if rotated:
            print(f"  (已存在，先归档 -> {rotated})")
        print(f"-> /backup/{index}  ->  {name}")
        try:
            size = backup_partition(args.router, index, out,
                                    port=args.web_port, timeout=300)
            print(f"   {size} 字节  [OK]")
            ok += 1
        except Exception as e:
            print(f"   [FAIL] {e}")
            fail += 1
    print(f"\n完成: 成功 {ok}，失败 {fail}。文件在 {BACKUP_DIR}")


def cmd_web_flash(args):
    from lib.http_flash import flash_bootloader
    model = resolve_model(args)
    bl2_def, fip_def = find_bl2_fip(model)
    bl2 = args.bl2 or bl2_def
    fip = args.fip or fip_def
    if not os.path.isfile(bl2):
        print(f"[FAIL] bl2 (preloader) 未找到: {bl2}\n"
              f"  请将 preloader 文件放入 {model_uboot_dir(model)}/（文件名含 preloader 即可）")
        return 1
    if not os.path.isfile(fip):
        print(f"[FAIL] fip (bl31-u-boot) 未找到: {fip}\n"
              f"  请将 fip 文件放入 {model_uboot_dir(model)}/（文件名含 fip 即可）")
        return 1
    print("即将刷写：")
    print(f"  bl2 (preloader)    = {bl2}  ({os.path.getsize(bl2)} 字节)")
    print(f"  fip (bl31-u-boot)  = {fip}  ({os.path.getsize(fip)} 字节)")
    print(f"  目标: POST http://{args.router}:{args.web_port}/api/flash")
    if not args.yes:
        print(f"\n⚠ 刷错引导会变砖，请确认 bl2/fip 文件对应 {DEVICE_MODELS[model]['name']}！")
        input("按回车继续，Ctrl+C 取消...")
    try:
        resp = flash_bootloader(args.router, bl2, fip,
                                port=args.web_port, timeout=120)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        return 1
    print(f"\n响应: {resp}")
    print("\n[OK] 刷写完成，约 800ms 后设备自动重启。")

    # 探测设备是否进入 U-Boot 恢复页（刷引导成功与否的关键验证，默认 20s）
    uboot_host = getattr(args, "uboot_host", None) or DEFAULT_UBOOT_HOST
    uboot_timeout = getattr(args, "uboot_timeout", None) or DEFAULT_UBOOT_TIMEOUT
    wait_uboot_recovery(uboot_host, uboot_timeout)

    print("\n下一步：")
    print("  1. 恢复页就绪后，回主菜单选 6（uboot-flash，1-5 步可重复执行）")
    print(f"     或浏览器手动访问 http://{uboot_host}")
    print("  2. 如恢复页始终起不来，拆机用 TTL 模式刷")
    return 0


def _find_itb(model=None):
    """在机型固件目录找 sysupgrade.itb（*.itb）。"""
    import glob
    d = model_uboot_dir(model)
    m = sorted(glob.glob(os.path.join(d, "*.itb")))
    return m[0] if m else None


def cmd_uboot_flash(args):
    from lib.uboot_flash import run_step, try_reboot, UbootError, _get_task
    import time
    import urllib.request
    host = args.uboot_host
    model = resolve_model(args)
    bl2_def, fip_def = find_bl2_fip(model)
    bl2 = args.bl2 or bl2_def
    fip = args.fip or fip_def
    bosa = args.bosa
    if not bosa:
        cand = os.path.join(BACKUP_DIR, "mtd6-bosa.bin")
        if os.path.isfile(cand):
            bosa = cand
    ri = getattr(args, "ri", None)
    if not ri:
        cand = os.path.join(BACKUP_DIR, "mtd7-ri.bin")
        if os.path.isfile(cand):
            ri = cand
    itb = args.itb or _find_itb(model)

    # 5 步定义：(编号, 名称, 文件或None, run_step 关键字参数)
    # 任意一步都可独立重复执行
    # 第3步 = 板级数据两项：bosa（校准）+ ri（新款 U-Boot 新增，MD/TF 都要刷）
    steps_def = [
        (1, "刷 preloader (flash-bl2)", bl2,
         dict(operation="flash-bl2", timeout=120)),
        (2, "重建 UBI (ubi-rebuild)", None,
         dict(operation="ubi-rebuild", timeout=300)),
        (3, "刷板级数据 (flash-board-data: bosa + ri)", None,
         dict(operation="flash-board-data", timeout=120)),
        (4, "刷 U-Boot (flash-uboot)", fip,
         dict(operation="flash-uboot", timeout=120)),
        (5, "刷系统固件 (flash)", itb,
         dict(operation="flash", timeout=900)),
    ]

    # 文件检查：缺文件的步骤标记为不可用（不直接退出，其他步骤仍可执行）
    unavailable = {}
    for num, label, path, kw in steps_def:
        if path and not os.path.isfile(path):
            unavailable[num] = path
    # 第3步需要 bosa + ri 两个备份文件（新 U-Boot 增加 ri 写入）
    missing_bd = [
        f"mtd{6 if t == 'bosa' else 7}-{t}.bin"
        for t, p in (("bosa", bosa), ("ri", ri))
        if not p or not os.path.isfile(p)
    ]
    if missing_bd:
        unavailable[3] = "、".join(os.path.join(BACKUP_DIR, f) for f in missing_bd)

    # 前置：恢复页可达
    print(f"目标: http://{host}  （U-Boot 恢复页，设备须处于恢复模式）")
    try:
        urllib.request.urlopen(f"http://{host}/", timeout=5)
        print("[OK] 恢复页可达")
    except Exception:
        print(f"[FAIL] http://{host}/ 不可达。")
        print("  设备须处于 U-Boot 恢复模式（192.168.0.x 网段）")
        return 1
    try:
        st = _get_task(host, 0)
        if st.get("busy"):
            print(f"[FAIL] 设备正忙: {st}")
            return 1
    except Exception:
        pass  # 状态探测失败不阻塞，各步骤自身会校验

    def do_board_data():
        """第3步：刷 bosa + ri 板级数据（先 bosa 后 ri，各自上传+下发任务）。"""
        t0 = time.time()
        try:
            run_step(host, "第3步a 刷 bosa 板级数据 (flash-board-data)",
                     upload_path=bosa, operation="flash-board-data",
                     target="bosa", timeout=120)
            run_step(host, "第3步b 刷 ri 板级数据 (flash-board-data)",
                     upload_path=ri, operation="flash-board-data",
                     target="ri", timeout=120)
        except UbootError as e:
            print(f"\n[FAIL] 第 3 步失败: {e}")
            print("  设备仍在 U-Boot 恢复模式，排查后可在菜单重新执行该步骤。")
            return False
        print(f"[OK] 第 3 步「刷板级数据 (bosa + ri)」成功（耗时 {time.time()-t0:.0f}s）")
        return True

    def do_step(num):
        """执行单步，成功返回 True。"""
        _, label, path, kw = steps_def[num - 1]
        if num in unavailable:
            print(f"[FAIL] 第 {num} 步所需文件不存在: {unavailable[num]}")
            if num == 3:
                print("  请先完成备份（bosa 模式会同时备份 mtd6-bosa.bin 和 mtd7-ri.bin）")
            return False
        if num == 3:
            return do_board_data()
        t0 = time.time()
        try:
            run_step(host, f"第{num}步 {label}", upload_path=path, **kw)
        except UbootError as e:
            print(f"\n[FAIL] 第 {num} 步失败: {e}")
            print("  设备仍在 U-Boot 恢复模式，排查后可在菜单重新执行该步骤。")
            return False
        print(f"[OK] 第 {num} 步「{label}」成功（耗时 {time.time()-t0:.0f}s）")
        return True

    def do_reboot():
        """触发重启并等待新系统上线。"""
        print("触发设备重启...")
        if try_reboot(host):
            print("  重启指令已下发")
        else:
            print("  未能自动重启（不影响结果），请手动断电重启")
        print("等待系统上线（PonWrt 默认 http://192.168.1.1，约 1~2 分钟）...")
        for i in range(24):
            time.sleep(5)
            try:
                urllib.request.urlopen("http://192.168.1.1/", timeout=3)
                print(f"[OK] 系统已上线（第 {(i + 1) * 5}s）: http://192.168.1.1")
                return
            except Exception:
                continue
        print("[—] 120s 内未检测到系统上线，请手动检查设备状态")

    # 非交互：--step N 只执行单步
    if args.step:
        return 0 if do_step(args.step) else 1

    # 交互菜单：任意步骤可重复执行
    done = set()
    while True:
        print(f"\n{'='*56}")
        print(f" U-Boot 刷机菜单  目标: http://{host}")
        print(f"{'='*56}")
        for num, label, path, kw in steps_def:
            if num in unavailable:
                mark = "✗缺文件"
            elif num in done:
                mark = "✓ 已完成"
            else:
                mark = "  未执行"
            print(f"  {num}. [{mark}] {label}")
        print("  a.  全部 1→5 顺序执行（中途失败即中止）")
        print("  r.  重启设备（5 步全部刷完后）")
        print("  0.  退出（不重启，之后可手动断电重启）")
        try:
            choice = input("\n请选择 [0-5/a/r]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。全部刷完后请手动断电重启设备。")
            return 0

        if choice == "0":
            print("已退出。全部刷完后请手动断电重启设备。")
            return 0
        elif choice in ("1", "2", "3", "4", "5"):
            num = int(choice)
            if do_step(num):
                done.add(num)
        elif choice == "a":
            if not args.yes:
                try:
                    input("将按序执行 1→5，任意一步失败即中止。回车开始，Ctrl+C 取消...")
                except (EOFError, KeyboardInterrupt):
                    print("\n已取消。")
                    continue
            all_ok = True
            for num in range(1, 6):
                if num == 1 and args.skip_bl2:
                    continue
                if num == 3 and args.skip_bosa:
                    continue
                if num == 4 and args.skip_uboot:
                    continue
                if not do_step(num):
                    all_ok = False
                    break
                done.add(num)
            if all_ok:
                print("\n[OK] 5 步全部完成，请选 r 重启或手动断电重启。")
        elif choice == "r":
            do_reboot()
        else:
            print("无效选择，请重新输入。")


def _prompt_model(default):
    """交互选择机型，返回机型 key（如 MD/TF）。回车沿用默认。"""
    keys = sorted(DEVICE_MODELS)
    print("\n请选择机型：")
    for i, k in enumerate(keys, 1):
        mark = "（当前默认）" if k == default else ""
        print(f"  {i}. {k}  {DEVICE_MODELS[k]['name']}{mark}")
    try:
        raw = input(f"请选择 [1-{len(keys)}]（回车默认 {default}）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n使用默认机型 {default}")
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    print(f"无效输入，使用默认机型 {default}")
    return default


def cmd_menu(args):
    """无子命令时进入的交互式主菜单：先选机型，再 1-6 步任选、可重复执行。

    4/5 步自动内联启动 HTTP 服务；第 5 步自动先 run-stock2ubi 再 web-flash。
    """
    # 复制全局参数并补全各子命令依赖的属性（菜单 args 只有全局参数）
    a = argparse.Namespace(**vars(args))
    sub_defaults = dict(
        root=None, port=DEFAULT_TFTP_PORT, web_port=DEFAULT_WEB_PORT,
        web_user=None, web_pass=None, password=None,
        model=None,
        user=None, user_pass=None, su_pass=None,
        backup_mode=None,
        bl2=None, fip=None, bosa=None, itb=None,
        uboot_host=DEFAULT_UBOOT_HOST, uboot_timeout=DEFAULT_UBOOT_TIMEOUT,
        step=None, yes=False,
        skip_bl2=False, skip_bosa=False, skip_uboot=False,
    )
    for k, v in sub_defaults.items():
        if not hasattr(a, k):
            setattr(a, k, v)

    def step_web_flash():
        """第 5 步编排：先在光猫拉起 :3333（run-stock2ubi），再刷引导。"""
        if cmd_run_stock2ubi(a) != 0:
            print("\n[FAIL] an758x-stock2ubi 未就绪，已中止刷引导。")
            return 1
        return cmd_web_flash(a) or 0

    # (编号, 名称, 处理函数)
    items = [
        (1, "检查环境", lambda: cmd_check(a)),
        (2, "开启 telnet（Web 登录自动开）", lambda: cmd_enable_telnet(a)),
        (3, "查询 SuPassword 超密", lambda: cmd_fetch_supass(a)),
        (4, "备份 mtd 分区（可选 bosa校准+ri 或全量，dd|curl）", lambda: cmd_backup(a)),
        (5, "刷引导（自动 run-stock2ubi → web-flash）", step_web_flash),
        (6, "U-Boot 恢复页刷机（192.168.0.1，5 步菜单）",
         lambda: cmd_uboot_flash(a)),
    ]
    by_num = {n: fn for n, _, fn in items}
    done = set()

    print("=" * 60)
    print("         AN758x-免拆刷机助手")
    print("=" * 60)
    # 先选机型，再进主菜单（CLI --model 指定则跳过交互选择）
    model = resolve_model(args)
    if not args.model:
        model = _prompt_model(model)
    a.model = model               # 菜单内所有步骤按所选机型执行
    write_env("DEVICE_MODEL", model)  # 记住选择，下次运行默认使用
    cur_name = DEVICE_MODELS[model]['name']
    print(f"[OK] 当前机型: {model} {cur_name}（已记入 .env，下次默认使用）")
    while True:
        print(f"\n{'='*60}\n 刷机主菜单（机型: {model} {cur_name}）\n{'='*60}")
        for num, name, _ in items:
            mark = "✓" if num in done else " "
            print(f"  {num}. [{mark}] {name}")
        print("  0. 退出")
        try:
            choice = input("\n请选择 [0-6]（新手按 1→6 顺序）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if choice == "0":
            print("再见。")
            return 0
        # input() 返回 str，by_num 键是 int，需先规范化再比较
        if not choice.isdigit() or int(choice) not in by_num:
            print("无效选择，请输入 1-6，或 0 退出。")
            continue
        num = int(choice)
        try:
            rc = by_num[num]()
        except KeyboardInterrupt:
            print("\n已中断该步骤，返回主菜单。")
            continue
        except Exception as e:
            print(f"\n[FAIL] 步骤异常: {e}")
            rc = 1
        if rc is None or rc == 0:
            done.add(num)
            print(f"\n—— 第 {num} 步完成 ——")
        else:
            done.discard(num)
            print(f"\n—— 第 {num} 步未成功，可重新选择该步重试 ——")
        try:
            input("回车返回主菜单...")
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0


def main():
    p = argparse.ArgumentParser(
        description="AN758x 光猫免拆刷机助手（XG-040G-MD / XG-040G-MF / XG-040G-TF，跨平台，纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="流程详见文件顶部注释。先 doctor，再 fetch-supass，依次往下。",
    )
    p.add_argument("--router", default=DEFAULT_ROUTER_IP,
                   help=f"路由器 IP（默认 {DEFAULT_ROUTER_IP}）")
    p.add_argument("--tftp-ip", default=DEFAULT_TFTP_IP,
                   help=f"本机 TFTP 服务器 IP（默认 {DEFAULT_TFTP_IP}，备用）")
    p.add_argument("--http-ip", default=None,
                   help=f"本机 HTTP 服务器 IP（不指定则检查环境时选择，"
                        f"否则默认 {DEFAULT_HTTP_IP}）")
    p.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP 服务器端口（默认 {DEFAULT_HTTP_PORT}）")
    p.add_argument("--model", default=None, choices=sorted(DEVICE_MODELS),
                   help=f"机型（决定 Web 默认密码，默认 .env DEVICE_MODEL 或 {DEFAULT_MODEL}）")
    # 无子命令 → 进入交互主菜单；子命令保留给高级/脚本化用法
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("check", help="环境自检").set_defaults(func=cmd_check)
    sub.add_parser("net-help", help="三平台设多 IP 命令清单").set_defaults(func=cmd_net_help)
    pe = sub.add_parser("enable-telnet", help="Web 登录光猫自动开启 telnet 服务")
    pe.add_argument("--web-user", default=None, help="Web 用户名（默认按机型，回车用机型默认）")
    pe.add_argument("--web-pass", default=None, help="Web 密码（默认按机型提示，见 --model）")
    pe.set_defaults(func=cmd_enable_telnet)
    pf = sub.add_parser("fetch-supass", help="Web 登录光猫提取 SuPassword（MD 日志明文 / MF、TF dumpdatamodel+AES）")
    pf.add_argument("--user", default=None, help="telnet 用户名（TF 机型无需）")
    pf.add_argument("--password", default=None, help="telnet 密码（TF 机型无需）")
    pf.set_defaults(func=cmd_fetch_supass)

    pl = sub.add_parser("login", help="测试 telnet 登录到 root #")
    pl.add_argument("--user", default=None, help="user 用户名")
    pl.add_argument("--user-pass", default=None, help="user 密码")
    pl.add_argument("--su-pass", default=None, help="SU 超密")
    pl.set_defaults(func=cmd_login)

    pt = sub.add_parser("tftpd", help="启动 TFTP 服务器（需 sudo，备用）")
    pt.add_argument("--root", default=None, help=f"TFTP 根目录（默认项目根 {PROJECT_DIR}）")
    pt.add_argument("--port", type=int, default=DEFAULT_TFTP_PORT)
    pt.set_defaults(func=cmd_tftpd)

    ph = sub.add_parser("httpd", help=f"启动 HTTP 服务器（默认端口 {DEFAULT_HTTP_PORT}，不需 sudo）")
    ph.add_argument("--root", default=None, help=f"HTTP 根目录（默认项目根 {PROJECT_DIR}）")
    ph.set_defaults(func=cmd_httpd)

    pb = sub.add_parser("backup", help="【原厂】dd|curl -T 备份 mtd（可选仅 bosa 或全量）到 backup/")
    pb.add_argument("--mode", dest="backup_mode", choices=["bosa", "full"],
                    default=None, help="备份范围：bosa=仅关键分区 bosa校准+ri（默认交互选择）；full=按 /proc/mtd 全量")
    pb.add_argument("--user", default=None)
    pb.add_argument("--user-pass", default=None)
    pb.add_argument("--su-pass", default=None)
    pb.set_defaults(func=cmd_backup)

    pr = sub.add_parser("run-stock2ubi", help="【原厂】curl 下载并执行 an758x-stock2ubi")
    pr.add_argument("--user", default=None)
    pr.add_argument("--user-pass", default=None)
    pr.add_argument("--su-pass", default=None)
    pr.set_defaults(func=cmd_run_stock2ubi)

    pwb = sub.add_parser("web-backup", help="GET :3333/backup/N 批量备份")
    pwb.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    pwb.set_defaults(func=cmd_web_backup)

    pwf = sub.add_parser("web-flash", help="POST :3333/api/flash 刷引导，并探测恢复页就绪")
    pwf.add_argument("--bl2", default=None, help="preloader 文件路径")
    pwf.add_argument("--fip", default=None, help="bl31-u-boot fip 文件路径")
    pwf.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    pwf.add_argument("--uboot-host", default=DEFAULT_UBOOT_HOST,
                     help=f"刷后探测的 U-Boot 恢复页地址（默认 {DEFAULT_UBOOT_HOST}）")
    pwf.add_argument("--uboot-timeout", type=int, default=DEFAULT_UBOOT_TIMEOUT,
                     help=f"恢复页探测超时秒数（默认 {DEFAULT_UBOOT_TIMEOUT}）")
    pwf.add_argument("--yes", action="store_true", help="跳过确认")
    pwf.set_defaults(func=cmd_web_flash)

    puf = sub.add_parser("uboot-flash",
                         help="U-Boot 恢复页全自动刷机（5 步，默认 192.168.0.1）")
    puf.add_argument("--uboot-host", default="192.168.0.1",
                     help="U-Boot 恢复页地址（默认 192.168.0.1）")
    puf.add_argument("--bl2", default=None, help="preloader 文件路径")
    puf.add_argument("--fip", default=None, help="bl31-u-boot fip 文件路径")
    puf.add_argument("--bosa", default=None,
                     help="mtd6-bosa.bin 路径（默认找 backup/）")
    puf.add_argument("--ri", default=None,
                     help="mtd7-ri.bin 路径（默认找 backup/，第3步与 bosa 一起刷）")
    puf.add_argument("--itb", default=None, help="sysupgrade.itb 路径（默认找 uboot/）")
    puf.add_argument("--step", type=int, choices=[1, 2, 3, 4, 5], default=None,
                     help="非交互：只执行指定的单一步骤（1-5），不进菜单")
    puf.add_argument("--skip-bl2", action="store_true", help="选 a 全部执行时跳过第 1 步")
    puf.add_argument("--skip-bosa", action="store_true", help="选 a 全部执行时跳过第 3 步")
    puf.add_argument("--skip-uboot", action="store_true", help="选 a 全部执行时跳过第 4 步")
    puf.add_argument("--yes", action="store_true", help="选 a 全部执行时跳过确认")
    puf.set_defaults(func=cmd_uboot_flash)

    args = p.parse_args()
    try:
        if getattr(args, "func", None):
            rc = args.func(args)          # 子命令：高级/脚本化用法
        else:
            rc = cmd_menu(args)           # 无子命令：交互主菜单
        sys.exit(rc or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
