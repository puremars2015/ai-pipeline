"""工作流的檔案存取層。

重點在兩件事：id 現在會變成檔名（而且來自網址），以及「一個檔案壞掉不能
讓整份清單消失」—— 那會讓使用者以為自己的東西全部不見了。
"""

from __future__ import annotations

import json

import pytest

from store.workflows import WorkflowError, WorkflowStore, safe_id, slugify

GRAPH = {
    "name": "測試流程",
    "nodes": [{"id": "a", "type": "mock", "config": {"prompt": "p"}}],
    "edges": [],
}


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "workflows")


# ------------------------------------------------------------ 路徑安全


@pytest.mark.parametrize(
    "bad",
    [
        "../../.ssh/authorized_keys",
        "../escape",
        "a/b",
        "a\\b",
        "..",
        "",
        "  ",
        ".hidden",
        "a:b",
        "a*b",
        "a\x00b",
        "x" * 200,
    ],
)
def test_dangerous_ids_are_rejected(store, bad):
    with pytest.raises(WorkflowError):
        safe_id(bad)
    with pytest.raises(WorkflowError):
        store.get(bad)


def test_traversal_cannot_delete_outside_the_folder(store, tmp_path):
    """delete 會 unlink，所以這是最需要擋住的那一個。"""
    victim = tmp_path / "victim.json"
    victim.write_text("{}", "utf-8")

    with pytest.raises(WorkflowError):
        store.delete("../victim")
    assert victim.exists()


def test_rejects_instead_of_silently_fixing(store):
    """不做「盡力修正」：使用者要求刪 '../x' 而我們默默改成刪 'x'，
    就刪掉了他們沒有要刪的東西。"""
    with pytest.raises(WorkflowError, match="路徑分隔符"):
        safe_id("../x")


def test_good_ids_pass():
    for ok in ("plan-impl-qa", "wf-20260818-120000-abc123", "a.b_c-1", "x",
               "規劃 → 實作 → QA", "Deploy (staging)"):
        assert safe_id(ok) == ok


# ------------------------------------------------------------ CRUD


def test_save_get_delete(store):
    wf_id = store.save(GRAPH)
    assert store.get(wf_id)["name"] == "測試流程"
    assert [w["id"] for w in store.list()] == [wf_id]

    store.save({**GRAPH, "id": wf_id, "name": "改名了"})
    assert store.get(wf_id)["name"] == "改名了"
    assert len(store.list()) == 1, "同 id 應該是覆寫不是新增一個檔案"

    assert store.delete(wf_id) is True
    assert store.get(wf_id) is None
    assert store.delete(wf_id) is False


def test_id_comes_from_name(store):
    assert store.save({**GRAPH, "name": "Plan Impl QA"}) == "Plan Impl QA"


def test_chinese_name_becomes_a_readable_filename(store, tmp_path):
    """中文名稱不轉 ascii —— 否則整個資料夾會是 workflow.json、
    workflow-a3f2.json，誰都看不出哪個是哪個。"""
    wf_id = store.save({**GRAPH, "name": "規劃與實作"})

    assert wf_id == "規劃與實作"
    assert (tmp_path / "workflows" / "規劃與實作.json").exists()
    assert store.get(wf_id)["name"] == "規劃與實作"


def test_chinese_id_survives_macos_filename_normalisation(store):
    """macOS 會把檔名正規化成 NFD。不統一成 NFC 的話，存完馬上用同一個
    字串去讀會讀不到。"""
    wf_id = store.save({**GRAPH, "name": "規劃 → 實作 → QA"})

    assert store.get(wf_id) is not None
    assert [w["id"] for w in store.list()] == [wf_id]
    assert store.delete(wf_id) is True


def test_name_with_slash_is_cleaned_not_nested(store, tmp_path):
    """名稱裡的斜線不能變成子目錄。"""
    wf_id = store.save({**GRAPH, "name": "a/b"})

    assert "/" not in wf_id
    assert (tmp_path / "workflows" / f"{wf_id}.json").exists()


def test_same_name_twice_does_not_overwrite(store):
    first = store.save({**GRAPH, "name": "重複"})
    second = store.save({**GRAPH, "name": "重複"})

    assert first != second
    assert len(store.list()) == 2


def test_filename_is_the_real_id(store, tmp_path):
    """檔案被改名之後，內文那個 id 就過期了 —— 以檔名為準。"""
    store.save({**GRAPH, "id": "original"})
    (tmp_path / "workflows" / "original.json").rename(
        tmp_path / "workflows" / "renamed.json"
    )

    assert store.get("renamed")["id"] == "renamed"
    assert store.get("original") is None


def test_saved_json_is_reviewable(store, tmp_path):
    """這個檔案會進 git，diff 要能看。擠成一行的話 review 不了。"""
    wf_id = store.save({**GRAPH, "name": "中文名稱"})
    raw = (tmp_path / "workflows" / f"{wf_id}.json").read_text("utf-8")

    assert raw.count("\n") > 3, "應該有縮排"
    assert "中文名稱" in raw, "不該被逃逸成 \\uXXXX"
    assert raw.endswith("\n")


# ------------------------------------------------------------ 壞掉的檔案


def test_broken_file_does_not_hide_the_others(store, tmp_path):
    """手改壞一個 json 就看不到其他所有工作流，是最糟的失敗方式。"""
    store.save({**GRAPH, "id": "good"})
    (tmp_path / "workflows" / "broken.json").write_text("{ 這不是 json", "utf-8")

    listed = {w["id"]: w for w in store.list()}
    assert set(listed) == {"good", "broken"}
    assert listed["broken"]["broken"] is True
    assert not listed["good"].get("broken")


def test_reading_a_broken_file_says_which_one(store, tmp_path):
    (tmp_path / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workflows" / "broken.json").write_text("{ nope", "utf-8")

    with pytest.raises(WorkflowError, match="broken.json"):
        store.get("broken")


def test_missing_folder_lists_empty(tmp_path):
    """剛註冊的專案還沒有 workflows/，不該炸掉。"""
    assert WorkflowStore(tmp_path / "nope").list() == []


def test_list_is_newest_first(store, tmp_path):
    import os
    import time

    store.save({**GRAPH, "id": "old"})
    store.save({**GRAPH, "id": "new"})
    old = tmp_path / "workflows" / "old.json"
    os.utime(old, (time.time() - 600, time.time() - 600))

    assert [w["id"] for w in store.list()] == ["new", "old"]


def test_slugify_keeps_readable_characters():
    assert slugify("Café Deploy") == "Café Deploy"
    assert slugify("  spaced   out  ") == "spaced out"
    assert slugify("a/b:c") == "a-b-c"
