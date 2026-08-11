"""Adapter 契約測試：argv 組裝規則、registry 載入、可選參數丟棄。"""

from __future__ import annotations

import pytest
import yaml

from adapters.base import AdapterError, AdapterField, AdapterSpec, render
from adapters.registry import ADAPTER_DIR, Registry


# ---------------------------------------------------------------- render


def test_render_substitutes_and_reports_empty():
    assert render("a-{{ x }}", {"x": "1"}) == ("a-1", False)
    assert render("a-{{ x }}", {"x": ""}) == ("a-", True)
    assert render("a-{{ x }}", {}) == ("a-", True)
    assert render("plain", {}) == ("plain", False)


def test_render_dotted_lookup():
    variables = {"nodes": {"qa": {"verdict": "PASS"}}}
    assert render("{{ nodes.qa.verdict }}", variables) == ("PASS", False)
    assert render("{{ nodes.qa.missing }}", variables) == ("", True)
    assert render("{{ nodes.nope.deep }}", variables) == ("", True)


def test_render_false_counts_as_empty():
    """bool False 視為未設定，這樣 {flag: [...]} 才能自然表達開關。"""
    assert render("{{ x }}", {"x": False}) == ("", True)


# ---------------------------------------------------------------- argv


def spec(**kw) -> AdapterSpec:
    base = dict(id="t", label="t", binary="bin")
    base.update(kw)
    return AdapterSpec(**base)


def test_plain_strings_always_kept():
    s = spec(argv=["exec", "--json"])
    assert s.build_argv({}) == ["bin", "exec", "--json"]


def test_flag_group_dropped_when_placeholder_empty():
    s = spec(argv=[{"flag": ["-m", "{{ model }}"]}])
    assert s.build_argv({"model": ""}) == ["bin"]
    assert s.build_argv({}) == ["bin"]
    assert s.build_argv({"model": "opus"}) == ["bin", "-m", "opus"]


def test_flag_group_all_or_nothing():
    """群組內任一 placeholder 為空 → 整組丟棄，不會留下半個 flag。"""
    s = spec(argv=[{"flag": ["--from", "{{ a }}", "--to", "{{ b }}"]}])
    assert s.build_argv({"a": "1"}) == ["bin"]
    assert s.build_argv({"a": "1", "b": "2"}) == ["bin", "--from", "1", "--to", "2"]


def test_bad_argv_entry_rejected():
    with pytest.raises(AdapterError, match="argv 項目"):
        spec(argv=[123]).build_argv({})
    with pytest.raises(AdapterError, match="flag 必須是 list"):
        spec(argv=[{"flag": "-m"}]).build_argv({})


def test_cwd_must_render():
    s = spec(cwd="{{ workdir }}")
    assert s.build_cwd({"workdir": "/tmp/x"}) == "/tmp/x"
    with pytest.raises(AdapterError, match="cwd 模板渲染成空"):
        s.build_cwd({})
    assert spec().build_cwd({}) is None


def test_invalid_spec_fields():
    with pytest.raises(AdapterError, match="prompt_delivery"):
        spec(prompt_delivery="carrier-pigeon")
    with pytest.raises(AdapterError, match="events"):
        spec(events="xml")
    with pytest.raises(AdapterError, match="type 不合法"):
        AdapterField(name="x", type="hologram")


# ---------------------------------------------------------------- registry


def test_registry_loads_shipped_adapters():
    r = Registry()
    assert {"codex", "claude", "opencode", "shell", "mock"} <= set(
        s.id for s in r.all()
    )


def test_registry_rejects_unknown_yaml_key(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        yaml.safe_dump({"id": "bad", "binary": "x", "wat": 1})
    )
    with pytest.raises(AdapterError, match="無法識別的欄位"):
        Registry(tmp_path)


def test_registry_requires_binary(tmp_path):
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump({"id": "bad"}))
    with pytest.raises(AdapterError, match="缺少必要欄位 binary"):
        Registry(tmp_path)


def test_registry_unknown_adapter_lists_options():
    r = Registry()
    with pytest.raises(AdapterError, match="未知的 adapter"):
        r.get("pi-code")


def test_registry_loads_normalizers():
    r = Registry()
    for aid in ("codex", "claude", "opencode", "mock"):
        assert callable(r.normalizer(r.get(aid)))
    # shell 沒有 normalizer，走純文字
    assert r.normalizer(r.get("shell")) is None


# ------------------------------------------------- 實際 adapter 的組裝結果


def test_codex_argv_matches_probed_invocation():
    """對照 tools/probe 實測成功的那組參數。"""
    s = Registry().get("codex")
    argv = s.build_argv({**s.defaults(), "workdir": "/wt"})
    assert argv == [
        "codex", "exec", "--json", "--color", "never",
        "-C", "/wt", "-s", "workspace-write",
    ]
    assert s.prompt_delivery == "stdin"


def test_claude_uses_cwd_not_flag():
    """claude 沒有 --cd，工作目錄只能靠 cwd。"""
    s = Registry().get("claude")
    argv = s.build_argv({**s.defaults(), "workdir": "/wt", "prompt": "P"})
    assert "--cd" not in argv and "/wt" not in argv
    assert s.build_cwd({"workdir": "/wt"}) == "/wt"
    assert argv[:6] == ["claude", "-p", "P", "--output-format", "stream-json", "--verbose"]


def test_claude_schema_is_inline_json_codex_is_file():
    """兩者的結構化輸出機制不同，adapter 各自挑自己要的變數。"""
    reg = Registry()
    variables = {
        "workdir": "/wt",
        "prompt": "P",
        "schema_file": "/tmp/s.json",
        "schema_json": '{"type":"object"}',
    }
    claude_argv = reg.get("claude").build_argv({**reg.get("claude").defaults(), **variables})
    codex_argv = reg.get("codex").build_argv({**reg.get("codex").defaults(), **variables})

    assert "--json-schema" in claude_argv and '{"type":"object"}' in claude_argv
    assert "--output-schema" in codex_argv and "/tmp/s.json" in codex_argv
    assert "--output-schema" not in claude_argv
    assert "--json-schema" not in codex_argv


def test_opencode_argv():
    s = Registry().get("opencode")
    argv = s.build_argv({**s.defaults(), "workdir": "/wt", "prompt": "P"})
    assert argv == ["opencode", "run", "P", "--format", "json", "--dir", "/wt"]


def test_shell_argv():
    s = Registry().get("shell")
    argv = s.build_argv({**s.defaults(), "workdir": "/wt", "command": "pytest -q"})
    assert argv == ["bash", "-lc", "pytest -q"]


def test_every_shipped_adapter_declares_workdir():
    """每個 adapter 都必須有辦法把工作目錄指到 worktree，否則會誤改別的地方。"""
    r = Registry()
    for s in r.all():
        argv_text = yaml.safe_dump(s.argv)
        assert s.cwd or "workdir" in argv_text, f"{s.id} 沒有指定工作目錄的途徑"


def test_shipped_yaml_files_parse():
    for path in ADAPTER_DIR.glob("*.yaml"):
        assert yaml.safe_load(path.read_text("utf-8")), f"{path.name} 空的"
