"""telnetlib 封装：登录光猫到 root # 提示符，发送命令并捕获输出。

跨平台（macOS/Linux/Windows 均自带 telnetlib，Python 3.13 起需改用 telnetlib3）。
"""
import re
import telnetlib
import time


class TelnetSession:
    """光猫 telnet 会话。

    主登录方式：login_admin（CMCCAdmin 直接登录到 root #，无需 su）。
    兼容旧路径：login_user + su（user → $ → su → #，适用于 CMCCAdmin 不能直接登录的机型）。
    命令执行：发命令 + echo 唯一标记，读到标记即视为完成，输出干净。
    """

    def __init__(self, host, port=23, timeout=20, log=True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = log
        self.tn = None

    def connect(self):
        if self.log:
            print(f"[telnet] 连接 {self.host}:{self.port} ...")
        self.tn = telnetlib.Telnet(self.host, self.port, self.timeout)
        if self.log:
            print("[telnet] 已连接")

    def _expect(self, pattern, timeout=None):
        """等待正则匹配。返回匹配到的全部缓冲。失败抛 TimeoutError/EOFError。"""
        t = timeout if timeout is not None else self.timeout
        if isinstance(pattern, str):
            pattern = pattern.encode()
        if not hasattr(pattern, "pattern"):
            pattern = re.compile(pattern)
        try:
            idx, _, data = self.tn.expect([pattern], timeout=t)
        except EOFError:
            raise EOFError("telnet 连接被设备关闭（可能 telnet 未开启或有残留会话）")
        if idx == -1:
            raise TimeoutError(f"等待 {pattern.pattern!r} 超时（{t}s），收尾: {data[-200:]!r}")
        return data

    def connect_login_user(self, user, user_pass, su_pass=None, retries=3):
        """连接 + login_user (+ su)，带 EOFError 自动重试。

        设备 telnet 服务刚开有时不稳定，连接后立即断开，
        重试 2~3 次通常能成功。
        """
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                if self.tn:
                    self.close()
                self.connect()
                time.sleep(0.5)
                self.login_user(user, user_pass)
                if su_pass:
                    self.su(su_pass)
                return True
            except (EOFError, OSError) as e:
                last_err = e
                if attempt < retries:
                    print(f"[telnet] 连接或登录失败（{e}），2s 后重试（{attempt}/{retries}）...")
                    time.sleep(2)
        raise RuntimeError(
            f"telnet 登录失败（重试 {retries} 次）: {last_err}\n"
            "  可能原因：1) telnet 未开启（先跑 enable-telnet）"
            "  2) 有残留 telnet 会话占用"
        )

    def connect_login(self, mode, user, user_pass, su_pass=None, retries=3):
        """按机型登录取向连接：mode='root' 走 root+超密直登（TF）；否则 user(+su)（MD）。

        设备 telnet 服务刚开有时不稳定，连接后立即断开，重试 2~3 次通常能成功。
        """
        if mode != "root":
            return self.connect_login_user(user, user_pass, su_pass, retries=retries)
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                if self.tn:
                    self.close()
                self.connect()
                time.sleep(0.5)
                self.login_root(su_pass)
                return True
            except (EOFError, OSError) as e:
                last_err = e
                if attempt < retries:
                    print(f"[telnet] 连接或登录失败（{e}），2s 后重试（{attempt}/{retries}）...")
                    time.sleep(2)
        raise RuntimeError(
            f"telnet 登录失败（重试 {retries} 次）: {last_err}\n"
            "  可能原因：1) telnet 未开启（先跑 enable-telnet）"
            "  2) 有残留 telnet 会话占用 3) SU 超密不正确"
        )

    def login_admin(self, admin_user, admin_pass):
        """超管直接登录到 root (# 提示符)。CMCCAdmin 登录即 root，无需 su。"""
        self._expect(rb"[Ll]ogin[: ]*")
        self.tn.write(admin_user.encode() + b"\n")
        if self.log:
            print(f"[telnet] 已发送用户名: {admin_user}")

        self._expect(rb"[Pp]assword[: ]*")
        self.tn.write(admin_pass.encode() + b"\n")
        if self.log:
            print("[telnet] 已发送超管密码")

        self._expect(rb"# *$", timeout=10)
        if self.log:
            print("[telnet] 登录成功，已进入 root (#)")
        return True

    def login_root(self, root_pass):
        """TF 路径：用户名固定 root + SU 超密直登（提示符 $ 或 #）。"""
        self._expect(rb"[Ll]ogin[: ]*")
        self.tn.write(b"root\n")
        if self.log:
            print("[telnet] 已发送用户名: root")

        self._expect(rb"[Pp]assword[: ]*")
        self.tn.write(root_pass.encode() + b"\n")
        if self.log:
            print("[telnet] 已发送 SU 超密")

        self._expect(rb"[$#] *$", timeout=10)
        if self.log:
            print("[telnet] 登录成功，已进入 root shell")
        return True

    def login(self, admin_user, admin_pass):
        """主登录入口：超管直接登录到 root (#)。"""
        return self.login_admin(admin_user, admin_pass)

    def login_user(self, user, user_pass):
        """兼容旧路径：仅登录到 user 普通 shell（$ 提示符）。"""
        self._expect(rb"[Ll]ogin[: ]*")
        self.tn.write(user.encode() + b"\n")
        if self.log:
            print(f"[telnet] 已发送用户名: {user}")

        self._expect(rb"[Pp]assword[: ]*")
        self.tn.write(user_pass.encode() + b"\n")
        if self.log:
            print("[telnet] 已发送 user 密码")

        self._expect(rb"[$#] *$", timeout=10)
        return True

    def su(self, su_pass):
        """兼容旧路径：在 user shell 里 su 提权到 root #。"""
        self.tn.write(b"su\n")
        if self.log:
            print("[telnet] 已发送 su")

        self._expect(rb"[Pp]assword[: ]*")
        self.tn.write(su_pass.encode() + b"\n")
        if self.log:
            print("[telnet] 已发送 su 密码")

        self._expect(rb"# *$", timeout=10)
        if self.log:
            print("[telnet] 登录成功，已进入 root (#)")
        return True

    def _sync_prompt(self, attempts=5, timeout=6):
        """标记握手：确认 shell 真正就绪后才继续发命令。

        TF 真机实测：登录提示符匹配可能提前返回（motd 未打完），
        此时立即发命令会被登录进程吞掉（复现率 4/5）。
        握手发一条 echo 唯一标记，读到标记输出才证明 shell 可执行命令；
        未就绪则重试。握手失败最终抛错，避免静默丢失命令。
        """
        for _ in range(attempts):
            marker = f"__FLASH_READY_{int(time.time() * 1000) % 100000}__"
            self.tn.read_very_eager()
            self.tn.write(b"\necho " + marker.encode() + b"\n")
            try:
                self._expect(re.escape(marker.encode()), timeout=timeout)
                self.tn.read_very_eager()
                return True
            except TimeoutError:
                time.sleep(1)
        raise RuntimeError(
            "shell 握手失败：设备无响应（可能登录未完成或会话异常），请重跑本步")

    def run(self, cmd, timeout=300, retries=3):
        """发送命令并等待结束。返回命令输出（字节）。

        实现：先握手确认 shell 就绪，再发命令 + echo 唯一标记，读至标记即为输出结束。
        TF 真机实测：即使握手通过，个别情况下首条命令仍会被设备吞掉
        （输出里没有命令回显）。因此校验回显存在性，缺失则重发（自愈）。
        长命令（如 dd|tftp 备份）可用大 timeout。
        """
        probe = cmd.strip()[:12]  # 命令回显探针（被吞时输出里不会有这段）
        last_data = b""
        for attempt in range(1, retries + 1):
            self._sync_prompt()
            marker = (f"__FLASH_END_{int(time.time() * 1000)}"
                      f"_{hash((cmd, attempt)) & 0xffff:04x}__")
            # 清空缓冲里残留；命令与标记单次写入，缩小行间竞态窗口
            self.tn.read_very_eager()
            self.tn.write(cmd.encode() + b"\necho " + marker.encode() + b"\n")
            try:
                data = self._expect(re.escape(marker.encode()), timeout=timeout)
            except TimeoutError:
                if attempt < retries:
                    print(f"[telnet] 命令超时无响应（{attempt}/{retries}），重发...")
                    continue
                raise
            if probe.encode() not in data and attempt < retries:
                # 命令被吞（无回显）：输出不可信，重新握手后重发
                print(f"[telnet] 疑似命令被设备吞掉（无回显，{attempt}/{retries}），重发...")
                last_data = data
                continue
            last_data = data
            break
        data = last_data
        # 去掉标记行本身（marker 出现位置之后的 echo 输出）
        idx = data.rfind(marker.encode())
        if idx == -1:
            return data
        head = data[:idx]  # head 不包含 marker
        first_nl = head.find(b"\n")
        if first_nl != -1:
            head = head[first_nl + 1:]  # 去掉第一行（命令回显）
        # 过滤标记残渣行（TTY 回显的 "echo __FLASH_END_xxx__" 会污染输出判断）
        return b"\n".join(
            ln for ln in head.split(b"\n")
            if b"__FLASH_END_" not in ln and b"__FLASH_READY_" not in ln
        )

    def close(self):
        if self.tn:
            try:
                self.tn.write(b"exit\n")
            except Exception:
                pass
            self.tn.close()
            self.tn = None
