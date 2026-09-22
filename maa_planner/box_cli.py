"""Explicit experimental Box commands, independent of daily and recovery."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import warnings
from pathlib import Path

from .skland import BoxError, SklandBoxProvider, SklandClient, text_field
from .sources import SourceError, _load_json_bytes
from .util import atomic_write_json, canonical_json, sha256_bytes


def secret_path(root: Path) -> Path:
    # Do not follow redirected runtime/secret directories when storing credentials.
    for directory in (root / "var", root / "var" / "secrets"):
        if directory.is_symlink():
            raise BoxError("storage", "凭据目录不能是符号链接。")
        directory.mkdir(mode=0o700, exist_ok=True)
    directory = root / "var" / "secrets"
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BoxError("storage", "var/secrets 必须由当前用户拥有，权限为 700。")
    return directory / "skland.json"


def save_secret(root: Path, kind: str, value: str) -> None:
    target = secret_path(root)
    if target.is_symlink():
        raise BoxError("storage", "凭据文件不能是符号链接。")
    if kind not in ("token", "cred"):
        raise BoxError("storage", "不支持的凭据类型。")
    atomic_write_json(target, {"schema": 1, kind: text_field(value)}, mode=0o600)


def load_secret(root: Path) -> dict:
    target = secret_path(root)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise BoxError("authentication", "尚未登录，请在本机终端运行 ./bin/zootd box-login。") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise BoxError("storage", "凭据必须是当前用户拥有、权限为 600 的普通文件。")
        raw = handle.read(16385)
    try:
        secret = _load_json_bytes(raw)
    except (SourceError, ValueError):
        raise BoxError("storage", "本地凭据文件损坏，请重新登录。") from None
    if (len(raw) > 16384 or not isinstance(secret, dict) or type(secret.get("schema")) is not int
            or secret["schema"] != 1 or set(secret) not in ({"schema", "token"}, {"schema", "cred"})):
        raise BoxError("storage", "本地凭据格式不支持，请重新登录。")
    kind = "token" if "token" in secret else "cred"
    return {kind: text_field(secret[kind])}


def visible_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise BoxError("input", "请在本机交互终端运行登录；不接受管道或聊天输入。")
    return input(prompt).strip()


def hidden_input(prompt: str) -> str:
    # getpass must never silently fall back to echoed stdin / captured tool output.
    if not sys.stdin.isatty():
        raise BoxError("input", "请在本机交互终端运行登录；不接受管道或聊天输入。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt).strip()
        except getpass.GetPassWarning:
            raise BoxError("input", "终端无法隐藏输入，登录已停止。") from None


def login(root: Path, client: SklandClient, mode: str) -> None:
    secret_path(root)  # Check local storage before sending an SMS.
    if mode == "sms":
        phone = visible_input("森空岛手机号：")
        client.send_code(phone)
        print("短信已发送一次。若需额外风控验证，请先在官方森空岛完成。")
        code = visible_input("短信验证码：")
        value = client.login(phone, code)
        kind = "token"
    else:
        kind = mode
        value = hidden_input("鹰角登录 token（输入隐藏）：" if mode == "token" else "Skland cred（输入隐藏）：")
    client.authenticate(**{kind: value})
    save_secret(root, kind, value)
    print("森空岛登录成功。凭据已保存在本机私有文件；可执行 ./bin/zootd box-sync。")


def sync(root: Path, client: SklandClient, uid: str | None) -> dict:
    credentials = client.authenticate(**load_secret(root))
    box = SklandBoxProvider(client, credentials, uid).fetch_box()
    payload = box.to_dict()
    # Failure before this point preserves the previous snapshot. No cache fallback.
    target = root / "var" / "state" / "operator-box.json"
    atomic_write_json(target, payload, mode=0o600)
    return {"status": "ok", "experimental": True, "source": box.source,
            "fetched_at": box.fetched_at, "operator_count": len(box.operators),
            "sha256": sha256_bytes(canonical_json(payload)), "path": str(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental, explicit Skland Box tools; never starts the game")
    parser.add_argument("--project-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("box-login", help="Interactive phone/SMS login; tokens stay local")
    modes = auth.add_mutually_exclusive_group()
    modes.add_argument("--token", action="store_true", help="Import a token using hidden terminal input")
    modes.add_argument("--cred", action="store_true", help="Import a Skland cred using hidden terminal input")
    fetch = commands.add_parser("box-sync", help="Fetch normalized Box without starting the game")
    fetch.add_argument("--uid", help="Choose one bound Official account when more than one exists")
    args = parser.parse_args(argv)
    try:
        root = args.project_root.resolve()
        client = SklandClient()
        if args.command == "box-login":
            login(root, client, "token" if args.token else "cred" if args.cred else "sms")
        else:
            print(json.dumps(sync(root, client, args.uid), ensure_ascii=False))
        return 0
    except BoxError as exc:
        print(json.dumps({"status": "error", "category": exc.category, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("登录/同步已取消。", file=sys.stderr)
        return 130
    except OSError:
        print('{"status":"error","category":"storage","message":"本地文件操作失败；未输出私有数据。"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
