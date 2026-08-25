"""python -m agent_godot.mcp.servers.godot —— stdio 起服入口。"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-godot-mcp",
                                     description="Godot MCP 服务器 (stdio)")
    parser.add_argument("--root", default=None,
                        help="Godot 项目根（默认 $AGENT_GODOT_ROOT 或当前目录）")
    parser.add_argument("--godot-bin", default=None,
                        help="Godot 可执行文件（默认 $GODOT_BIN 或 PATH 查找）")
    args = parser.parse_args()
    from .server import serve
    serve(Path(args.root) if args.root else None, args.godot_bin)


if __name__ == "__main__":
    main()
