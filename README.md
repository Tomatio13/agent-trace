# Agent Trace

Claude Code、Codex CLI、OpenCode のローカルセッションログを、ブラウザで開ける単一の HTML にします。タイムラインで処理にかかった時間やエラーの発生箇所を確認できます。Claude Code と Codex CLI では、親セッションとサブエージェントの活動を既定で同じ時間軸に表示します。

![Codex CLI の親セッションとサブエージェントを同じ時間軸に並べ、選択したイベントの本文を右側に表示した Agent Trace の画面](assets/agent-trace-screenshot.png)

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

Claude Code・Codex CLI では、選択したセッションと配下のサブエージェントをまとめます。選択したセッションだけを表示するには `--no-include-subagents` を付けてください。OpenCode は選択したセッションを表示します。

```bash
python3 scripts/generate_trace.py --source codex --session SESSION_ID \
  --output /tmp/agent-trace.html
```

出力先の既定値はカレントディレクトリの `trace.html` です。同じ出力先で再実行するとファイルを更新します。別の場所から実行するときは `scripts/generate_trace.py` の絶対パスを指定してください。生成スクリプトはスキル内の `assets/viewer.html` を読み込みます。

## スキルとして使う

スキルを追加するには、`agent-trace` ディレクトリごと、利用するエージェントのスキル検索先へ配置します。

> agent-trace でこのプロジェクトの最新セッションを表示して。時間がかかった処理や、同じ結果を繰り返した箇所があれば説明して。

スキルも同じ Python スクリプトで HTML を生成します。追加解説では、実行中の AI が診断候補から元ログの該当箇所を確認します。CLI から診断結果も保存したい場合は `--diagnostics /tmp/diagnostics.json` を付けてください。[スキルの実行手順](SKILL.md)

## タイムラインの読み方

- **時間** — 全体の経過時間と処理の所要時間は `HH:MM:SS.mmm` で表示します。横軸の目盛りは秒単位です。バーは開始と終了が分かる処理、点は時刻だけが分かるイベントです。終了したターンと次のターンの間に活動記録がなければ、その空白を時間軸から除きます。
- **親と子の動き** — 親子をまとめて表示すると、画面上部にセッションごとの行が並びます。子セッションは親の下に字下げされ、各行を同じ時刻で見比べると、並行して動いた時間帯が分かります。エージェント名をクリックすると、下のイベント一覧をそのエージェントに絞り込めます。
- **イベント詳細** — 一覧で気になる行を選ぶと、会話の本文や、ログに残っているファイルパス、コマンド、終了コード、失敗時の出力を確認できます。親子をまとめて表示した場合も、各イベントの詳細から該当する元ログをたどれます。
- **ユーザーへの質問** — Claude Code の `AskUserQuestion` と Codex の `request_user_input` は、行に質問を表示します。詳細には選択肢とログに残っている回答が出ます。
- **ファイル操作** — 記録された読み取り・書き込み・編集の対象パスを行に表示します。絞り込みで「ファイル操作」を選ぶか、対象パスを検索できます。Codex のファイル変更イベントでは、記録された追加・編集・削除と移動先も確認できます。
- **エラーと反復** — エラーや繰り返し候補を選ぶとイベント詳細が開きます。反復は、同一ターンで同じ引数と結果の操作が続いたことを示します。意図したポーリングかどうかは、実行内容から判断できます。
- **実行の内訳** — Claude Code ではモデルAPIエラー、記録されたターンの所要時間、Read の開始行と行数を確認できます。Codex では記録された最初のトークンまでの時間、MCP の成否、中断理由を表示します。サブエージェントの行を開くと、親セッションに記録された依頼や返答が読めます。
- **使用量** — 「セッション情報」にトークンの内訳とログに記録された金額を表示します。親子をまとめて表示した場合は、エージェントごとの内訳を確認できます。金額や反復を判定する材料が欠けている場合は、その旨を表示します。

`--metadata-only` を付けると会話本文と操作の詳細を省けます。ツールの入力と出力まで含めるには `--include-content` を指定します。

## ディレクトリ構成

```text
agent-trace/
├── README.md
├── SKILL.md
├── scripts/
│   └── generate_trace.py
├── assets/
│   ├── agent-trace-screenshot.png
│   ├── document.css
│   ├── document.css.LICENSE
│   └── viewer.html
└── tests/
    └── test_generate_trace.py
```

`SKILL.md` をルートに置く [Agent Skills の仕様](https://agentskills.io/specification) に沿った構成です。テストはルートで `python3 -B -m unittest` を実行します。
