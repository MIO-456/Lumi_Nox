"""Run with python -m public_host. No model/device connection until a UI action."""
import argparse
from pathlib import Path

from public_host.config import ConfigStore, LOCAL_DIR
from public_host.providers import ProviderError
from public_host.server import HostApp, make_server


def main():
    parser = argparse.ArgumentParser(description="Lumi_Nox 通用公开宿主：本机配置与对话控制页")
    parser.add_argument("--port", type=int, default=8765, help="本机控制页端口（默认 8765）")
    parser.add_argument("--data-dir", type=Path, default=LOCAL_DIR, help="独立本机配置目录")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port 必须在 0–65535 之间")
    app = HostApp(ConfigStore(args.data_dir))
    try:
        app.store.load()
        server = make_server(app=app, port=args.port)
    except (ProviderError, OSError):
        parser.exit(1, "无法启动控制页，请检查端口或本机配置文件。\n")
    print(f"公开宿主控制页：http://127.0.0.1:{server.server_port}", flush=True)
    print("首次可直接点“体验聊天”进入无声直播间；接真实服务请展开设置。Ctrl+C 退出。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
