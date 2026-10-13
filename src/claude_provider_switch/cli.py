"""Command line entry point: ccp."""

from __future__ import annotations

import argparse
import getpass
import sys

from . import __version__
from .core import (
    Paths,
    Provider,
    SwitchError,
    active_names,
    list_providers,
    load_provider,
    read_settings,
    remove_provider,
    save_provider,
    switch,
    validate_endpoint,
    validate_name,
)


def mask(token: str | None) -> str:
    if not token:
        return "(未设置)"
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "***"


def _read_token() -> str:
    if sys.stdin.isatty():
        token = getpass.getpass("Token（输入不回显）: ")
    else:
        token = sys.stdin.readline()
    return token.strip()


def cmd_list(paths: Paths, _args: argparse.Namespace) -> int:
    entries = list_providers(paths)
    if not entries:
        print(f"还没有供应商。用 ccp add 或 ccp save 创建，存放于 {paths.providers}")
        return 0
    try:
        active = set(active_names(paths, read_settings(paths)))
    except SwitchError:
        active = set()
    for name, item in entries:
        mark = "*" if name in active else " "
        if isinstance(item, Provider):
            print(f"{mark} {name:<20} {item.base_url}  {mask(item.token)}")
        else:
            print(f"! {name:<20} 配置损坏：{item}")
    return 0


def cmd_current(paths: Paths, _args: argparse.Namespace) -> int:
    settings = read_settings(paths)
    url, token = settings.endpoint
    names = active_names(paths, settings)
    print(f"供应商: {', '.join(names) if names else '(未匹配任何已保存的供应商)'}")
    print(f"地址:   {url or '(未设置)'}")
    print(f"Token:  {mask(token)}")
    return 0


def cmd_use(paths: Paths, args: argparse.Namespace) -> int:
    result = switch(paths, args.name, force=args.force)
    for warning in result.warnings:
        print(f"注意: {warning}", file=sys.stderr)
    if not result.changed:
        print(f"已经是 {args.name}，settings.json 未改动")
        return 0
    print(f"已切换到 {args.name}")
    print("新开的 Claude Code 会话生效；已运行的会话请重启。")
    return 0


def cmd_add(paths: Paths, args: argparse.Namespace) -> int:
    validate_name(args.name)
    validate_endpoint(args.url, "placeholder", args.name)
    token = _read_token()
    path = save_provider(paths, Provider(args.name, args.url, token), overwrite=args.force)
    print(f"已保存 {args.name} -> {path}")
    return 0


def cmd_save(paths: Paths, args: argparse.Namespace) -> int:
    url, token = read_settings(paths).endpoint
    validate_endpoint(url, token, "settings.json 当前配置")
    path = save_provider(paths, Provider(args.name, url, token), overwrite=args.force)
    print(f"已把当前地址和 token 保存为 {args.name} -> {path}")
    return 0


def cmd_rm(paths: Paths, args: argparse.Namespace) -> int:
    load_provider(paths, args.name)  # surfaces a clear error for unknown names
    remove_provider(paths, args.name)
    print(f"已删除 {args.name}（settings.json 未改动）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccp",
        description="切换 Claude Code 的 API 供应商。只改 settings.json 里的 env.ANTHROPIC_BASE_URL 和 env.ANTHROPIC_AUTH_TOKEN。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", aliases=["ls"], help="列出供应商，* 为当前生效").set_defaults(func=cmd_list)
    sub.add_parser("current", help="显示 settings.json 当前的地址和 token").set_defaults(func=cmd_current)

    use = sub.add_parser("use", help="切换到指定供应商")
    use.add_argument("name")
    use.add_argument("--force", action="store_true", help="忽略编辑器锁文件检查")
    use.set_defaults(func=cmd_use)

    add = sub.add_parser("add", help="新增供应商，token 通过隐藏输入或 stdin 读取")
    add.add_argument("name")
    add.add_argument("--url", required=True, help="ANTHROPIC_BASE_URL")
    add.add_argument("--force", action="store_true", help="覆盖同名供应商")
    add.set_defaults(func=cmd_add)

    save = sub.add_parser("save", help="把 settings.json 当前的地址和 token 存为供应商")
    save.add_argument("name")
    save.add_argument("--force", action="store_true", help="覆盖同名供应商")
    save.set_defaults(func=cmd_save)

    rm = sub.add_parser("rm", help="删除供应商（不改 settings.json）")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_rm)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(Paths.default(), args)
    except SwitchError as exc:
        print(f"ccp: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
