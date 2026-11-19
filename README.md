# Agent Trace

Claude Code、Codex CLI、OpenCode のローカルセッションログを、ブラウザで開ける単一の HTML にします。時間軸で処理の長さと重なりを確認し、気になる行を選ぶと、記録されたファイルパス、実行コマンド、終了コード、失敗時の出力を読めます。

![Agent Traceの実行タイムラインと、選択したイベントのモデル使用量を表示する詳細パネル](assets/agent-trace-screenshot.png)

## コマンドで使う

このディレクトリで実行します。必要なのは Python 3.10 以降とブラウザです。ログの解析と HTML の生成は Python の標準ライブラリで行い、画面の操作には HTML 内の JavaScript を使います。

```bash
python3 scripts/generate_trace.py --project /path/to/project \
  --output /tmp/agent-trace.html --open
```

`--project` には調べたいプロジェクトのパスを指定します。指定ディレクトリとその配下で記録されたセッションから、親セッションを優先して、表示できるイベントがある最新の1件を選びます。空のセッションをスキップした場合は、その件数を表示します。

| 調べたい対象 | 指定方法 |
| --- | --- |
| プロジェクト内の特定のエージェント | `--project PATH --source claude`（`codex`、`opencode` も指定可） |
| 特定のセッション | `--session ID`（完全一致） |
| Claude Code・Codex の JSONL、OpenCode のエクスポート JSON・実行 JSONL | `--file PATH` |
| OpenCode の SQLite にある特定のセッション | `--file /path/to/opencode.db --session ID` |

出力先の既定値はカレントディレクトリの `trace.html` です。同じ出力先で再実行するとファイルを更新します。別の場所から実行するときは `scripts/generate_trace.py` の絶対パスを指定してください。生成スクリプトはスキル内の `assets/viewer.html` を読み込みます。

## スキルとして使う

スキルを追加するには、`agent-trace` ディレクトリごと、利用するエージェントのスキル検索先へ配置します。

> agent-trace でこのプロジェクトの最新セッションを表示して。時間がかかった処理や、同じ結果を繰り返した箇所があれば説明して。

スキルも同じ Python スクリプトで HTML を生成します。追加解説では、実行中の AI が診断候補から元ログの該当箇所を確認します。CLI から診断結果も保存したい場合は `--diagnostics /tmp/diagnostics.json` を付けてください。[スキルの実行手順](SKILL.md)

## タイムラインの読み方

- **時間** — バーは開始と終了が分かる処理、点は時刻だけが分かるイベントです。終了したターンと次のターンの間に活動記録がなければ、その空白を時間軸から除きます。ターン内の待機や並行処理はそのまま表示します。
- **エラーと反復** — エラーや繰り返し候補を選ぶとイベント詳細が開きます。反復は、同一ターンで同じ引数と結果の操作が続いたことを示します。意図したポーリングかどうかは、実行内容から判断できます。
- **使用量** — 「セッション情報」にトークンの内訳とログに記録された金額を表示します。金額や反復を判定する材料が欠けている場合は、その旨を表示します。

既定の HTML には会話本文、対象パス、コマンド、失敗時の出力が入ります。会話の行には冒頭が表示され、行を選ぶと全文を読めます。`--metadata-only` を付けると会話本文と操作の詳細を省けます。ツールの入力と出力まで含めるには `--include-content` を指定します。

## ディレクトリ構成

```text
agent-trace/
├── README.md
├── SKILL.md
├── scripts/
│   └── generate_trace.py
├── assets/
│   ├── document.css
│   ├── document.css.LICENSE
│   └── viewer.html
└── tests/
    └── test_generate_trace.py
```

`SKILL.md` をルートに置く [Agent Skills の仕様](https://agentskills.io/specification) に沿った構成です。テストはルートで `python3 -B -m unittest` を実行します。
