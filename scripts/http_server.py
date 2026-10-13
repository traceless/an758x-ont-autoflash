#!/usr/bin/env python3
"""最简 HTTP 服务器（标准库），支持 GET 下载 + PUT 上传。

替代 TFTP 用于光猫刷机文件传输：
  - 下载（PC→光猫）：光猫 `curl -o /tmp/xxx http://192.168.1.254:7123/tools/xxx`
  - 上传（光猫→PC）：光猫 `dd if=/dev/mtd6 | curl -T - http://192.168.1.254:7123/backup/mtd6-bosa.bin`

优势：TCP 可靠传输、不需要 sudo（端口 7123）、有访问日志。
脚本退出时会 POST /__shutdown 让服务自我关闭，不留残留进程。
"""
import os
import sys
import time
import threading
import functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# 自关闭端点：脚本退出时 POST 它，让服务（含独立 httpd 进程）优雅退出
SHUTDOWN_PATH = "/__shutdown"


class HttpHandler(SimpleHTTPRequestHandler):
    """支持 GET（继承自带）+ PUT（自定义上传）的请求处理器。

    通过 SimpleHTTPRequestHandler 的 directory 参数（3.7+）指定根目录，
    无需 os.chdir，可安全地在主进程后台线程内联运行。
    """

    protocol_version = "HTTP/1.1"  # 支持长连接 + 100 Continue

    def handle_expect_100(self):
        """curl 管道上传（curl -T -）会发 Expect: 100-continue，
        必须回 100 Continue，否则 curl 等待 1s 超时后才发 body。"""
        self.send_response_only(100)
        self.end_headers()
        return True

    def _stream_body(self, out_f):
        """读取请求 body 并流式写入 out_f，返回字节数。

        兼容两种编码：
          - Content-Length: 普通定长 body
          - Transfer-Encoding: chunked: dd|curl -T - 管道上传用
        """
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            return self._stream_chunked(out_f)
        length = int(self.headers.get("Content-Length", 0))
        remaining = length
        while remaining > 0:
            buf = self.rfile.read(min(65536, remaining))
            if not buf:
                break
            out_f.write(buf)
            remaining -= len(buf)
        return length - remaining

    def _stream_chunked(self, out_f):
        """解码 chunked transfer encoding，边解码边写文件。

        格式： <hex size>\\r\\n <data>\\r\\n ... 0\\r\\n \\r\\n
        """
        total = 0
        while True:
            size_line = self.rfile.readline().rstrip(b"\r\n")
            if not size_line:
                break
            try:
                size = int(size_line.split(b";")[0], 16)  # 忽略 ;ext
            except ValueError:
                raise ValueError(f"非法 chunk 大小: {size_line!r}")
            if size == 0:
                # 读完 trailer（直到空行）
                while True:
                    line = self.rfile.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                break
            # 读 size 字节数据 + 紧随的 CRLF
            remaining = size
            while remaining > 0:
                buf = self.rfile.read(min(65536, remaining))
                if not buf:
                    break
                out_f.write(buf)
                remaining -= len(buf)
                total += len(buf)
            self.rfile.readline()
        return total

    def do_POST(self):
        """POST /__shutdown：脚本退出时通知 HTTP 服务自我关闭。

        必须先把 200 响应发完再 shutdown（在独立线程延迟执行），
        否则调用方收不到响应；serve_forever() 随即返回，独立 httpd 进程退出。
        """
        if self.path.split("?", 1)[0] != SHUTDOWN_PATH:
            self.send_error(404)
            return
        body = b"bye"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

        def _quit():
            time.sleep(0.2)  # 等响应发出
            try:
                self.server.shutdown()
            except Exception:
                pass
        threading.Thread(target=_quit, daemon=True).start()

    def do_PUT(self):
        """处理 PUT 上传：URL 路径即文件保存路径。

        安全策略：先写 .uploading 临时文件，校验非空成功后才归档旧文件
        并原子替换，避免上传失败/空 body 破坏现有好备份。
        """
        path = self.translate_path(self.path)

        # 防路径穿越（基准为 directory 参数，不依赖进程 cwd）
        root = os.path.abspath(self.directory)
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(root + os.sep) and abs_path != root:
            self.send_error(403, "Forbidden: path traversal")
            return

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        tmp_path = abs_path + ".uploading"

        received = 0
        try:
            with open(tmp_path, "wb") as f:
                received = self._stream_body(f)
            if received == 0:
                raise ValueError("收到 0 字节（chunked 管道未解析或 dd 无输出）")
        except Exception as e:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            # reason 走 latin-1 状态行，必须 ASCII；详情写进 UTF-8 body
            self.send_response(400, "Bad Upload")
            msg = f"upload failed: {e}\n".encode("utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            print(f"  [http] PUT 失败 {os.path.basename(abs_path)}: {e}")
            return

        # 上传成功：归档同名旧文件，再原子替换
        if os.path.isfile(abs_path):
            import time
            bak = abs_path + f".{int(time.time())}.bak"
            os.rename(abs_path, bak)
            print(f"  [http] 旧文件归档: {os.path.basename(bak)}")
        os.rename(tmp_path, abs_path)

        body = f"OK {received}\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"  [http] PUT {os.path.basename(abs_path)} ({received}B)")

    def log_message(self, format, *args):
        """访问日志。"""
        print(f"  [http] {self.address_string()} {format % args}")

    def end_headers(self):
        # 允许光猫 curl 访问
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def make_server(port, root, host="0.0.0.0"):
    """构造绑定 root 目录的多线程 HTTP 服务器（未启动，调用方自行 serve）。

    供 flash.py 在后台线程内联启动，避免用户另开终端。
    host 默认 0.0.0.0（全部网卡）；指定网卡 IP 可避开只占 127.0.0.1 的程序。
    """
    handler = functools.partial(HttpHandler, directory=os.path.abspath(root))
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7123
    root = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.getcwd()
    os.makedirs(os.path.join(root, "backup"), exist_ok=True)
    server = make_server(port, root)
    print(f"[http] listening on 0.0.0.0:{port}  root={root}")
    print(f"  下载: curl -o /tmp/xxx http://<本机IP>:{port}/path/to/file")
    print(f"  上传: dd if=/dev/mtdN | curl -T - http://<本机IP>:{port}/backup/name.bin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[http] 已停止")
