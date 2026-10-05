"""开发/演示入口：python -m scheduling.server [--host 127.0.0.1] [--port 8080]

服务为内存存储，重启即清空；生产部署可将 Repository 替换为持久实现，
service 层的事务语义与接口契约保持不变。
"""
from __future__ import annotations

import argparse

from .httpapi import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="主诊医师授权排班 HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    httpd = create_server(args.host, args.port)
    host, port = httpd.server_address[:2]
    print(f"主诊医师授权排班服务已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
