# Fixture 來源

normalizer 的測試輸入。**來源不同,可信度也不同**,這裡記清楚,免得日後誤以為
每一份都是實測的。

| 檔案 | 來源 | 怎麼重新產生 |
|---|---|---|
| `codex.jsonl` | **實跑抓的**(codex-cli 0.136.0,成功路徑) | `python -m tools.probe codex` |
| `codex.error.jsonl` | **實跑抓的** —— 本機 codex 版本比 `~/.codex/config.toml` 指定的模型舊,API 回 400 | 同上,在版本不相容的環境下 |
| `codex.noise.txt` | **實跑抓的** —— codex 混進 stdout 的純文字診斷訊息 | 同上 |
| `claude.jsonl` | **實跑抓的**(Claude Code 2.0.49) | `python -m tools.probe claude` |
| `opencode.jsonl` | **實跑抓的**(opencode 1.17.9) | `python -m tools.probe opencode` |
| `pi.noauth.jsonl` | **實跑抓的**(pi 0.84.1,未設定憑證) | `python -m tools.probe pi` |
| `pi.jsonl` | ⚠ **只有第一行是實跑抓的**,其餘依文件構造 | 見下 |

## pi.jsonl 為什麼是構造的

本機 pi 沒有設定任何 provider 憑證:

```
$ pi auth check --provider google --json
{"status":"not_ready","provider":"google","reason":"credentials_not_configured"}
```

所以跑不完整條流程 —— 串流只吐得出 session 標頭就以 exit 1 結束
(那份實測結果留在 `pi.noauth.jsonl`)。

其餘事件是依兩份**權威來源**構造的,不是憑印象猜的:

- `docs/json.md` —— pi 自己的 JSON 事件串流文件,列出完整的 event union
- `node_modules/@earendil-works/pi-ai/dist/types.d.ts` —— `AssistantMessage`、
  `ToolResultMessage`、`Usage`、`TextContent`、`ThinkingContent`、`ToolCall` 的
  實際 TypeScript 定義

工具名稱(`read` `write` `edit` `bash` `grep`)與參數名(`write` 用
`absolutePath` / `path`)是從 `dist/` 的實際程式碼抓出來的。

**設定好憑證之後請重跑一次 probe 覆蓋掉它:**

```bash
python -m tools.probe pi
python -m pytest tests/test_normalizers.py -q
```

測試若因此失敗,那正是它該做的事 —— 代表構造出來的形狀跟實際輸出有落差。
