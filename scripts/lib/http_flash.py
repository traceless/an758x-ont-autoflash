"""an758x-stock2ubi 的 :3333 HTTP 接口封装。

接口（源码确认）：
- GET  /backup/{index}  下载分区原始数据
- POST /api/flash       multipart 上传 bl2 + fip，返回 JSON
无 cookie/session，axum 实现，对 multipart boundary 不挑刺。
"""
import os
import uuid
import urllib.request
import urllib.error


def backup_partition(router_ip, index, output_file, port=3333, timeout=120):
    """GET /backup/{index} 下载分区到 output_file。返回字节数。"""
    url = f"http://{router_ip}:{port}/backup/{index}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = 0
        with open(output_file, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        return total


def _build_multipart(fields):
    """构造 multipart/form-data body。

    fields: dict[name] = (filename, bytes)
    返回 (body_bytes, content_type_header)
    """
    boundary = "----flash" + uuid.uuid4().hex
    body = bytearray()
    for name, (filename, data) in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        ).encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def flash_bootloader(router_ip, bl2_file, fip_file, port=3333, timeout=120):
    """POST /api/flash 刷写 bl2 (preloader) + fip (bl31-u-boot)。

    成功返回响应文本（JSON），失败抛 HTTPError 或 URLError。
    """
    url = f"http://{router_ip}:{port}/api/flash"
    with open(bl2_file, "rb") as f:
        bl2_data = f.read()
    with open(fip_file, "rb") as f:
        fip_data = f.read()

    body, content_type = _build_multipart({
        "bl2": (os.path.basename(bl2_file), bl2_data),
        "fip": (os.path.basename(fip_file), fip_data),
    })

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {body_text}") from None
