#!/usr/bin/env python3
"""最简 TFTP 服务器（RFC 1350 + 2347 选项协商），纯标准库，跨平台。

只实现刷机所需子集：
  - RRQ（下载）：光猫 `tftp -g -r tools/an758x-stock2ubi ...` 拉文件
  - WRQ（上传）：光猫 `dd | tftp -p -l - -r backup/mtdX.bin ...` 推文件
  - octet 模式、可变 blocksize、ACK 重传、子目录支持、路径防穿越

用法：
  sudo python3 tftp_server.py [根目录]     # 一般经 flash.py tftpd 调用
"""
import os
import socket
import sys
import threading

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6
DEFAULT_BLOCK = 512
TIMEOUT = 3.0
RETRY = 5


class TftpServer:
    def __init__(self, root, host="0.0.0.0", port=69):
        self.root = os.path.abspath(root)
        self.host = host
        self.port = port
        self.sock = None

    def _safe_path(self, name):
        """防路径穿越：规范化后必须仍在 root 内。"""
        name = name.replace("\\", "/").lstrip("/")
        p = os.path.abspath(os.path.join(self.root, name))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError(f"非法路径: {name}")
        return p

    def serve_forever(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        print(f"[tftp] listening on {self.host}:{self.port}  root={self.root}")
        while True:
            data, addr = self.sock.recvfrom(4096)
            threading.Thread(target=self._handle, args=(data, addr),
                             daemon=True).start()

    def _parse_rrq_wrq(self, data):
        """解析 RRQ/WRQ 包：filename, mode, options。"""
        # 去掉 2 字节 opcode
        parts = data[2:].split(b"\x00")
        # 过滤空字符串
        parts = [p for p in parts if p]
        if len(parts) < 2:
            return None, None, {}
        filename = parts[0].decode("utf-8", "ignore")
        mode = parts[1].decode("utf-8", "ignore").lower()
        # 剩余的是 option/value 对
        options = {}
        i = 2
        while i + 1 < len(parts):
            key = parts[i].decode("utf-8", "ignore").lower()
            val = parts[i + 1].decode("utf-8", "ignore")
            options[key] = val
            i += 2
        return filename, mode, options

    def _negotiate_options(self, options):
        """协商选项，返回 (支持的选项字典, blocksize)。"""
        ack_opts = {}
        blocksize = DEFAULT_BLOCK
        if "blksize" in options:
            try:
                bs = int(options["blksize"])
                # 限制在 512 ~ 1428（MTU 安全范围）
                bs = max(512, min(bs, 1428))
                ack_opts["blksize"] = str(bs)
                blocksize = bs
            except ValueError:
                pass
        if "tsize" in options:
            # 接受 tsize 选项，但不提供值（客户端会在 RRQ 里发 0）
            ack_opts["tsize"] = options["tsize"]
        return ack_opts, blocksize

    @staticmethod
    def _build_oack(options):
        """构造 OACK 包。"""
        pkt = OP_OACK.to_bytes(2, "big")
        for k, v in options.items():
            pkt += k.encode() + b"\x00" + v.encode() + b"\x00"
        return pkt

    @staticmethod
    def _build_error(code, msg):
        """构造 ERROR 包。"""
        return OP_ERROR.to_bytes(2, "big") + code.to_bytes(2, "big") + msg.encode() + b"\x00"

    def _handle(self, data, addr):
        if len(data) < 4:
            return
        op = int.from_bytes(data[:2], "big")
        try:
            filename, mode, options = self._parse_rrq_wrq(data)
            if filename is None:
                return
            path = self._safe_path(filename)
            ack_opts, blocksize = self._negotiate_options(options)
            if op == OP_RRQ:
                self._send_file(path, addr, ack_opts, blocksize)
            elif op == OP_WRQ:
                self._recv_file(path, addr, ack_opts, blocksize)
        except Exception as e:
            print(f"[tftp] {addr[0]} 请求处理失败: {e}")
            try:
                s = self._new_sock()
                s.sendto(self._build_error(0, str(e)), addr)
                s.close()
            except Exception:
                pass

    @staticmethod
    def _new_sock():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(TIMEOUT)
        return s

    def _send_file(self, path, addr, ack_opts, blocksize):
        """RRQ：把文件发给客户端，每块等 ACK，超时重传。"""
        if not os.path.isfile(path):
            print(f"[tftp] RRQ 未找到: {path}")
            s = self._new_sock()
            s.sendto(self._build_error(1, "File not found"), addr)
            s.close()
            return
        size = os.path.getsize(path)
        print(f"[tftp] RRQ {os.path.relpath(path, self.root)} ({size}B) -> {addr[0]} blksize={blocksize}")
        s = self._new_sock()
        try:
            with open(path, "rb") as f:
                block = 1
                # 如果有选项，先发 OACK，等 ACK0
                if ack_opts:
                    oack = self._build_oack(ack_opts)
                    got_ack = False
                    for _ in range(RETRY):
                        s.sendto(oack, addr)
                        try:
                            ack, _ = s.recvfrom(2048)
                        except socket.timeout:
                            continue
                        if (len(ack) >= 4
                                and int.from_bytes(ack[:2], "big") == OP_ACK
                                and int.from_bytes(ack[2:4], "big") == 0):
                            got_ack = True
                            break
                    if not got_ack:
                        raise TimeoutError("OACK 后未收到 ACK0")
                else:
                    # 无选项，直接从 block 1 开始
                    pass

                while True:
                    chunk = f.read(blocksize)
                    pkt = OP_DATA.to_bytes(2, "big") + block.to_bytes(2, "big") + chunk
                    got_ack = False
                    for _ in range(RETRY):
                        s.sendto(pkt, addr)
                        try:
                            ack, _ = s.recvfrom(2048)
                        except socket.timeout:
                            continue
                        if (len(ack) >= 4
                                and int.from_bytes(ack[:2], "big") == OP_ACK
                                and int.from_bytes(ack[2:4], "big") == block):
                            got_ack = True
                            break
                    if not got_ack:
                        raise TimeoutError(f"块 {block} 未收到 ACK")
                    if len(chunk) < blocksize:  # 末块（含 0 字节终包）
                        break
                    block = (block + 1) & 0xFFFF
            print(f"[tftp] RRQ 完成 {os.path.basename(path)} ({size}B)")
        except Exception as e:
            print(f"[tftp] RRQ 失败 {os.path.basename(path)}: {e}")
        finally:
            s.close()

    def _recv_file(self, path, addr, ack_opts, blocksize):
        """WRQ：收客户端数据，ACK 驱动下一块，<blocksize 字节为末块。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"[tftp] WRQ {os.path.relpath(path, self.root)} <- {addr[0]} blksize={blocksize}")
        s = self._new_sock()
        received = 0
        try:
            with open(path, "wb") as f:
                expected = 1
                last_ack = 0
                # 如果有选项，先发 OACK（等客户端发 DATA1）
                # 如果无选项，发 ACK0（等客户端发 DATA1）
                if ack_opts:
                    initial_pkt = self._build_oack(ack_opts)
                else:
                    initial_pkt = OP_ACK.to_bytes(2, "big") + (0).to_bytes(2, "big")
                s.sendto(initial_pkt, addr)

                while True:
                    pkt = None
                    for _ in range(RETRY):
                        try:
                            pkt, _ = s.recvfrom(4096)
                            break
                        except socket.timeout:
                            # 重发上一个包催客户端重传
                            s.sendto(initial_pkt if expected == 1
                                      else OP_ACK.to_bytes(2, "big")
                                      + last_ack.to_bytes(2, "big"), addr)
                    if pkt is None:
                        raise TimeoutError("收 DATA 超时")
                    if (len(pkt) < 4
                            or int.from_bytes(pkt[:2], "big") != OP_DATA):
                        continue
                    blk = int.from_bytes(pkt[2:4], "big")
                    if blk != expected:
                        continue  # 重复块，等正确序号
                    payload = pkt[4:]
                    f.write(payload)
                    received += len(payload)
                    last_ack = blk
                    s.sendto(OP_ACK.to_bytes(2, "big") + blk.to_bytes(2, "big"), addr)
                    if len(payload) < blocksize:  # 末块
                        break
                    expected = (expected + 1) & 0xFFFF
            print(f"[tftp] WRQ 完成 {os.path.basename(path)} ({received}B)")
        except Exception as e:
            print(f"[tftp] WRQ 失败 {os.path.basename(path)}: {e}")
            if received == 0 and os.path.isfile(path):
                os.unlink(path)  # 清理空壳文件
        finally:
            s.close()


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    try:
        TftpServer(root).serve_forever()
    except PermissionError:
        print("错误：端口 69 需要特权。macOS/Linux 用 sudo，Windows 用管理员。")
    except KeyboardInterrupt:
        print("\n[tftp] 已停止")
