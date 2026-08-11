"""探測 agent CLI 的實際事件輸出，存成 fixture。

三個 CLI 的 JSONL 欄位不能憑記憶推測 —— 版本一改就變。這支工具實跑一次，
把原始輸出存成 tests/fixtures/<id>.jsonl，並印出結構摘要，
normalizer 依實際觀察到的 schema 來寫。

用法：
    .venv/bin/python -m tools.probe codex
    .venv/bin/python -m tools.probe claude --prompt "建立 hello.txt 內容 hello"
    .venv/bin/python -m tools.probe --summarize tests/fixtures/codex.jsonl
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from adapters.registry import default as default_registry

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

DEFAULT_PROMPT = (
    "在目前的工作目錄建立一個檔案 hello.txt，內容就一行 hello。"
    "建立完成後回覆 done，不要做其他事。"
)


def make_scratch_repo() -> Path:
    """建立一個臨時 git repo 當探測場地（agent 需要 git repo 才肯動）。"""
    path = Path(tempfile.mkdtemp(prefix="probe-repo-"))
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "probe@example.com"],
        ["git", "config", "user.name", "Probe"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("scratch\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )
    return path


def probe(adapter_id: str, prompt: str, workdir: Path | None = None) -> Path:
    registry = default_registry()
    spec = registry.get(adapter_id)

    if not registry.is_installed(spec):
        print(f"✗ 找不到指令: {spec.binary}", file=sys.stderr)
        raise SystemExit(2)

    scratch = workdir or make_scratch_repo()
    variables = {
        **spec.defaults(),
        "workdir": str(scratch),
        "tool_root": str(ROOT),
        "prompt": prompt,
    }

    argv = spec.build_argv(variables, binary=registry.resolve_binary(spec))
    cwd = spec.build_cwd(variables) or str(scratch)

    print(f"→ 場地: {scratch}")
    print(f"→ 指令: {' '.join(argv)}")
    print(f"→ cwd:  {cwd}\n")

    stdin_data = prompt if spec.prompt_delivery == "stdin" else None

    err_path = FIXTURES / f"{adapter_id}.stderr.txt"
    FIXTURES.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    try:
        if stdin_data is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            preview = line[:160]
            print(f"  {preview}{'…' if len(line) > 160 else ''}")
    finally:
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()

    # 失敗的探測不要蓋掉既有的 fixture。實際踩過：pi 因為沒有憑證而失敗，
    # 只吐出一行 session 標頭，把原本完整的 fixture 整份蓋掉了。
    if code == 0:
        out_path = FIXTURES / f"{adapter_id}.jsonl"
    else:
        out_path = FIXTURES / f"{adapter_id}.failed.jsonl"

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if stderr.strip():
        err_path.write_text(stderr, encoding="utf-8")

    print(f"\n→ exit code: {code}")
    print(f"→ 已存: {out_path} ({len(lines)} 行)")
    if code != 0:
        good = FIXTURES / f"{adapter_id}.jsonl"
        print(f"  ⚠ 探測失敗，沒有覆蓋 {good.name}"
              + ("（它本來就不存在）" if not good.exists() else "（保留原本那份）"))
    if stderr.strip():
        print(f"→ stderr: {err_path}")
        print(f"  {stderr.strip().splitlines()[0][:120]}")
    print(f"→ hello.txt 存在: {(scratch / 'hello.txt').exists()}")

    summarize(out_path)
    return out_path


def _paths(obj: object, prefix: str = "") -> list[str]:
    """把巢狀 dict 攤平成點分隔的鍵路徑，附帶型別。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                found.extend(_paths(value, path))
            else:
                found.append(f"{path}:{type(value).__name__}")
    elif isinstance(obj, list):
        if obj:
            found.extend(_paths(obj[0], f"{prefix}[]"))
        else:
            found.append(f"{prefix}[]:empty")
    return found


def summarize(path: Path) -> None:
    """印出結構摘要，用來寫 normalizer。"""
    print(f"\n{'=' * 72}\n事件結構摘要: {path.name}\n{'=' * 72}")

    kinds: Counter[str] = Counter()
    shapes: dict[str, set[str]] = defaultdict(set)
    bad = 0

    for raw in path.read_text("utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(event, dict):
            bad += 1
            continue

        # 用最能區分事件種類的欄位組合當 key
        parts = [str(event.get(k)) for k in ("type", "subtype") if event.get(k)]
        item = event.get("item")
        if isinstance(item, dict) and item.get("type"):
            parts.append(f"item={item['type']}")
        key = " / ".join(parts) or "(no type field)"

        kinds[key] += 1
        shapes[key].update(_paths(event))

    for key, count in kinds.most_common():
        print(f"\n▸ {key}  ×{count}")
        for p in sorted(shapes[key]):
            print(f"    {p}")

    if bad:
        print(f"\n⚠ {bad} 行不是 JSON 物件（純文字輸出？）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("adapter", nargs="?", help="adapter id: codex / claude / opencode")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--workdir", type=Path, default=None)
    ap.add_argument("--summarize", type=Path, default=None, help="只摘要既有 fixture")
    args = ap.parse_args()

    if args.summarize:
        summarize(args.summarize)
        return
    if not args.adapter:
        ap.error("要指定 adapter 或 --summarize")
    probe(args.adapter, args.prompt, args.workdir)


if __name__ == "__main__":
    main()
