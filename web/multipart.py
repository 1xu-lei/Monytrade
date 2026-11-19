"""multipart/form-data 解析（标准库没有可用实现，这里手写一个够用的）。

只处理最简单的场景：一个或多个 part，每个 part 一个 name 字段，
文件 part 带 filename。不处理嵌套 multipart、不处理 base64 传输编码。
Python 3.13 起 cgi 模块被移除，所以这里必须自己解析。
"""

MAX_BODY_BYTES = 8 * 1024 * 1024


class MultipartError(Exception):
    pass


def boundary_of(content_type: str) -> bytes:
    for chunk in (content_type or "").split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("boundary="):
            value = chunk[len("boundary="):].strip()
            return value.strip('"').encode("latin-1")
    raise MultipartError("请求缺少 multipart boundary")


def parse(body: bytes, content_type: str) -> dict:
    """返回 {name: {'filename': str, 'data': bytes}}。"""
    boundary = boundary_of(content_type)
    delimiter = b"--" + boundary
    fields = {}
    for part in body.split(delimiter):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.startswith(b"--"):
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        if not _:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]

        name, filename = "", ""
        for line in head.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("content-disposition:"):
                for piece in line.split(";")[1:]:
                    key, _, value = piece.strip().partition("=")
                    value = value.strip().strip('"')
                    if key.lower() == "name":
                        name = value
                    elif key.lower() == "filename":
                        filename = value
        if not name:
            continue
        fields[name] = {"filename": filename, "data": payload}
    if not fields:
        raise MultipartError("没有解析到任何表单字段，请确认表单使用 multipart/form-data")
    return fields
