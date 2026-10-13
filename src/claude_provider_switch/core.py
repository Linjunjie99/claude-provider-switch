"""Provider storage and the settings.json patch.

The only keys ever written into settings.json are env.ANTHROPIC_BASE_URL and
env.ANTHROPIC_AUTH_TOKEN. Every other key keeps its value and position, and the
patched document is verified against the original before it replaces the file.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from json.decoder import scanstring
from pathlib import Path
from typing import Iterator

URL_KEY = "ANTHROPIC_BASE_URL"
TOKEN_KEY = "ANTHROPIC_AUTH_TOKEN"
MANAGED_KEYS = (URL_KEY, TOKEN_KEY)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BOM = b"\xef\xbb\xbf"


class SwitchError(Exception):
    """A refusal or failure that leaves settings.json untouched."""


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def default(cls) -> "Paths":
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        return cls(Path(env).expanduser() if env else Path.home() / ".claude")

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def providers(self) -> Path:
        return self.root / "providers"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    token: str

    def document(self) -> dict:
        # Same shape as a settings file, so `claude --settings <file>` also works.
        return {"env": {URL_KEY: self.base_url, TOKEN_KEY: self.token}}


@dataclass
class SwitchResult:
    changed: bool
    warnings: list[str] = field(default_factory=list)


# ---------- JSON helpers ----------


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"重复的键 {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> None:
    raise ValueError(f"不支持的数值 {name}")


def parse_json(text: str, source: str | Path) -> object:
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except ValueError as exc:  # JSONDecodeError is a ValueError subclass
        raise SwitchError(f"{source} 解析失败：{exc}") from None


def _detect_indent(text: str) -> str | None:
    if "\n" not in text.strip():
        return None  # single-line file stays compact
    match = re.search(r"\n([ \t]+)\S", text)
    return match.group(1) if match else "  "


def render_like(original: str, data: dict) -> str:
    """Serialize data using the original file's indent, line ending and final newline."""
    out = json.dumps(data, indent=_detect_indent(original), ensure_ascii=False)
    if original.endswith("\n"):
        out += "\n"
    if "\r\n" in original:
        # JSON strings cannot contain raw newlines, so every \n here is structural.
        out = out.replace("\n", "\r\n")
    return out


_WS = re.compile(r"[ \t\n\r]*")


def _skip_ws(text: str, i: int) -> int:
    return _WS.match(text, i).end()


def env_value_spans(text: str) -> dict[str, tuple[int, int]]:
    """Text spans of the top-level env.<managed key> values, quotes included.

    Only called on text that already parsed cleanly; any surprise returns {} so
    the caller falls back to a full re-render (which is still verified).
    """
    decoder = json.JSONDecoder()
    spans: dict[str, tuple[int, int]] = {}
    try:
        i = _skip_ws(text, 0)
        if text[i] != "{":
            return {}
        i = _skip_ws(text, i + 1)
        while text[i] == '"':
            key, i = scanstring(text, i + 1)
            i = _skip_ws(text, _skip_ws(text, i) + 1)  # past ':'
            if key == "env" and text[i] == "{":
                j = _skip_ws(text, i + 1)
                while text[j] == '"':
                    sub, j = scanstring(text, j + 1)
                    j = _skip_ws(text, _skip_ws(text, j) + 1)
                    _, end = decoder.raw_decode(text, j)
                    if sub in MANAGED_KEYS:
                        spans[sub] = (j, end)
                    j = _skip_ws(text, end)
                    if text[j] == ",":
                        j = _skip_ws(text, j + 1)
            _, i = decoder.raw_decode(text, i)
            i = _skip_ws(text, i)
            if text[i] == ",":
                i = _skip_ws(text, i + 1)
    except (IndexError, ValueError):
        return {}
    return spans


def render_patch(original: str, new_data: dict) -> str:
    """Replace the managed values in place when both exist; otherwise re-render."""
    spans = env_value_spans(original)
    if set(spans) != set(MANAGED_KEYS):
        return render_like(original, new_data)
    out = original
    for key, (start, end) in sorted(spans.items(), key=lambda kv: kv[1][0], reverse=True):
        out = out[:start] + json.dumps(new_data["env"][key], ensure_ascii=False) + out[end:]
    return out


def _atomic_write(path: Path, payload: bytes, mode: int, expect: bytes | None = None) -> None:
    """Write via temp file + rename. With expect, abort if path changed meanwhile."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.ccp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if expect is not None and path.read_bytes() != expect:
            raise SwitchError(f"{path} 在切换过程中被其他程序修改，已放弃，文件未改动")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ---------- providers ----------


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise SwitchError(f"名称 {name!r} 不合法：只能用字母、数字、. _ -，且以字母或数字开头")
    return name


def validate_endpoint(base_url: object, token: object, source: str) -> None:
    if not isinstance(base_url, str) or not re.match(r"^https?://\S+$", base_url):
        raise SwitchError(f"{source}：{URL_KEY} 必须是 http(s):// 开头的地址")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise SwitchError(f"{source}：{TOKEN_KEY} 不能为空或包含空白字符")


def provider_path(paths: Paths, name: str) -> Path:
    return paths.providers / f"{validate_name(name)}.json"


def load_provider(paths: Paths, name: str) -> Provider:
    path = provider_path(paths, name)
    if not path.is_file():
        raise SwitchError(f"没有名为 {name} 的供应商（{path}）")
    data = parse_json(path.read_text("utf-8"), path)
    env = data.get("env") if isinstance(data, dict) else None
    if set(data if isinstance(data, dict) else ()) != {"env"} or not isinstance(env, dict) or set(env) != set(MANAGED_KEYS):
        raise SwitchError(f'{path} 格式不对，应只包含 {{"env": {{"{URL_KEY}": ..., "{TOKEN_KEY}": ...}}}}')
    validate_endpoint(env[URL_KEY], env[TOKEN_KEY], str(path))
    return Provider(name, env[URL_KEY], env[TOKEN_KEY])


def list_providers(paths: Paths) -> list[tuple[str, Provider | SwitchError]]:
    if not paths.providers.is_dir():
        return []
    result: list[tuple[str, Provider | SwitchError]] = []
    for path in sorted(paths.providers.glob("*.json")):
        name = path.stem
        try:
            result.append((name, load_provider(paths, name)))
        except SwitchError as exc:
            result.append((name, exc))
    return result


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def save_provider(paths: Paths, provider: Provider, overwrite: bool = False) -> Path:
    validate_endpoint(provider.base_url, provider.token, provider.name)
    path = provider_path(paths, provider.name)
    if path.exists() and not overwrite:
        raise SwitchError(f"供应商 {provider.name} 已存在，覆盖请加 --force")
    _ensure_private_dir(paths.providers)
    text = json.dumps(provider.document(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write(path, text.encode("utf-8"), 0o600)
    return path


def remove_provider(paths: Paths, name: str) -> None:
    path = provider_path(paths, name)
    if not path.is_file():
        raise SwitchError(f"没有名为 {name} 的供应商（{path}）")
    path.unlink()


# ---------- settings.json ----------


@dataclass(frozen=True)
class Settings:
    path: Path  # symlinks resolved, so the rename replaces the real file
    raw: bytes
    text: str
    bom: bool
    data: dict
    mode: int

    @property
    def endpoint(self) -> tuple[str | None, str | None]:
        env = self.data.get("env") or {}
        return env.get(URL_KEY), env.get(TOKEN_KEY)


def read_settings(paths: Paths) -> Settings:
    if not paths.settings.is_file():
        raise SwitchError(f"找不到 {paths.settings}")
    real = paths.settings.resolve()
    raw = real.read_bytes()
    bom = raw.startswith(BOM)
    try:
        text = raw[len(BOM) if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError:
        raise SwitchError(f"{real} 不是 UTF-8 编码") from None
    data = parse_json(text, real)
    if not isinstance(data, dict):
        raise SwitchError(f"{real} 顶层不是 JSON 对象")
    if not isinstance(data.get("env", {}), dict):
        raise SwitchError(f"{real} 里的 env 不是 JSON 对象")
    return Settings(real, raw, text, bom, data, real.stat().st_mode & 0o7777)


def patch(data: dict, provider: Provider) -> dict:
    new = copy.deepcopy(data)
    env = new.setdefault("env", {})
    env[URL_KEY] = provider.base_url
    env[TOKEN_KEY] = provider.token
    return new


def _without_managed(data: dict) -> str:
    stripped = copy.deepcopy(data)
    env = stripped.get("env")
    if isinstance(env, dict):
        for key in MANAGED_KEYS:
            env.pop(key, None)
        if not env:
            del stripped["env"]
    return json.dumps(stripped, ensure_ascii=False)  # order-sensitive fingerprint


def verify_patch(old: dict, new_text: str, provider: Provider) -> None:
    new = parse_json(new_text, "生成的 settings.json")
    if not isinstance(new, dict) or _without_managed(old) != _without_managed(new):
        raise SwitchError("内部校验失败：除地址和 token 外的内容发生了变化，已放弃写入")
    env = new.get("env", {})
    if env.get(URL_KEY) != provider.base_url or env.get(TOKEN_KEY) != provider.token:
        raise SwitchError("内部校验失败：地址或 token 未正确写入，已放弃写入")


def editor_locks(path: Path) -> list[Path]:
    names = [f".{path.name}.swp", f".{path.name}.swo", f".{path.name}.swn", f".#{path.name}"]
    return [path.parent / n for n in names if os.path.lexists(path.parent / n)]


@contextlib.contextmanager
def _lock(paths: Paths) -> Iterator[None]:
    _ensure_private_dir(paths.providers)
    with open(paths.providers / ".lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SwitchError("另一个 ccp 正在运行") from None
        yield


def active_names(paths: Paths, settings: Settings) -> list[str]:
    url, token = settings.endpoint
    return [n for n, p in list_providers(paths) if isinstance(p, Provider) and (p.base_url, p.token) == (url, token)]


def switch(paths: Paths, name: str, force: bool = False) -> SwitchResult:
    provider = load_provider(paths, name)
    with _lock(paths):
        settings = read_settings(paths)
        locks = editor_locks(settings.path)
        if locks and not force:
            listed = "、".join(str(p) for p in locks)
            raise SwitchError(f"发现编辑器锁文件 {listed}，settings.json 可能正被编辑。关闭编辑器后重试，或加 --force")

        result = SwitchResult(changed=False)
        env = settings.data.get("env", {})
        if "ANTHROPIC_API_KEY" in env:
            result.warnings.append("settings.json 的 env 里还有 ANTHROPIC_API_KEY，ccp 不会改它，注意是否与 token 冲突")
        # Inside a Claude Code session these are inherited from settings.json itself.
        for key in MANAGED_KEYS if not os.environ.get("CLAUDECODE") else ():
            if key in os.environ:
                result.warnings.append(f"当前 shell 也导出了 {key}，可能影响实际生效的值")

        if settings.endpoint == (provider.base_url, provider.token):
            return result

        new_data = patch(settings.data, provider)
        new_text = render_patch(settings.text, new_data)
        verify_patch(settings.data, new_text, provider)
        payload = (BOM if settings.bom else b"") + new_text.encode("utf-8")
        _atomic_write(settings.path, payload, settings.mode, expect=settings.raw)
        result.changed = True
        return result
