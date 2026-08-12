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
| `pi.noauth.jsonl` | **實跑抓的**(pi 0.84.1,憑證未設定時) | `python -m tools.probe pi` |
| `pi.jsonl` | **實跑抓的**(pi 0.84.1,經 OpenRouter,`claude-haiku-4.5`,含 write 工具呼叫) | 見下 |

## pi.jsonl 的來源

本機沒有 google / anthropic / openai 的直接憑證,但 `~/.hermes/.env` 有一支
`OPENROUTER_API_KEY`,而 pi 認得 `--provider openrouter`:

```bash
source ~/.hermes/.env
pi -p --mode json --provider openrouter --model anthropic/claude-haiku-4.5 \
  "建立一個檔案 hello.txt，內容就一行 hello。建立完成後回覆 done" < /dev/null
```

這份 fixture 就是那次實跑的完整輸出(50 行,含 `tool_execution_start` /
`tool_execution_end` 的 write 呼叫、thinking、usage)。之前有一版是依
`docs/json.md` 與 `types.d.ts` 構造的(因為以為完全沒有可用的 provider),
已經用實測結果整份取代 —— 構造版只有 session 標頭是真的,其餘都是猜的。

**注意**:pi 即使指定 `-p`,不明確關閉 stdin 也可能無限等待。實跑時務必
`< /dev/null`(或讓呼叫端關閉 stdin,`engine/executor.py` 對 argv 傳遞的
adapter 就是這樣做的)。

CLI 升版或改了事件格式,重跑 probe 覆蓋掉它:

```bash
source ~/.hermes/.env
python -m tools.probe pi
python -m pytest tests/test_normalizers.py -q
```
