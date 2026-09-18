import argparse
import json
import sys
from pathlib import Path
from .core import run_tool, TOOLS_BY_NAME


def main():
    parser = argparse.ArgumentParser(description="AutoShop H3U 离线编辑与原厂无界面编译")
    parser.add_argument("tool", choices=list(TOOLS_BY_NAME) + ["serve"])
    parser.add_argument("--params-file", help="UTF-8 JSON 参数文件")
    args = parser.parse_args()
    if args.tool == "serve":
        from .server import main as serve
        return serve()
    try:
        params = json.loads(Path(args.params_file).read_text("utf-8-sig")) if args.params_file else {}
        result = run_tool(args.tool, params)
    except (ValueError, OSError) as exc:
        result = {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
