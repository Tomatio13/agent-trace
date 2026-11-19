import argparse
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.generate_trace import (
    diagnostic_events,
    diagnostic_report,
    pair_tools,
    public_events,
    render_html,
    select_trace,
)

EPOCH = 1_750_000_000
PRIVATE = "BODY-DO-NOT-EXPORT </ScRiPt><script>globalThis.injected = true</script>"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


def codex_records(cwd):
    def codex(t, kind, **payload):
        return {"type": kind, "timestamp": EPOCH + t, "payload": payload}

    records = [
        codex(
            0, "session_meta", id="test-codex", cwd=str(cwd), base_instructions=PRIVATE
        ),
        codex(0, "event_msg", type="task_started", turn_id="turn-1"),
        codex(1, "turn_context", turn_id="turn-1", model="demo-model"),
        codex(
            1, "response_item", type="message", role="user", content=[{"text": PRIVATE}]
        ),
    ]

    def call(t, tool, id_, args):
        return codex(
            t,
            "response_item",
            type="function_call",
            name=tool,
            call_id=id_,
            arguments=json.dumps(args),
        )

    def result(t, id_, output):
        return codex(
            t, "response_item", type="function_call_output", call_id=id_, output=output
        )

    # Overlap two tools, finish them out of order, then repeat the same two-step cycle.
    records.extend(
        [
            call(2, "read", "a0", {"path": PRIVATE}),
            call(3, "check", "b0", {}),
            result(5, "b0", {"exit_code": 1}),
            result(14, "a0", "same"),
        ]
    )
    for index, start in enumerate((18, 24), 1):
        records.extend(
            [
                call(start, "read", f"a{index}", {"path": PRIVATE}),
                result(start + 1, f"a{index}", "same"),
                call(start + 2, "check", f"b{index}", {}),
                result(start + 3, f"b{index}", {"exit_code": 1}),
            ]
        )
    records.extend(
        [
            call(30, "waiting", "open", {}),
            codex(
                169,
                "token_usage_record",
                response_id="resp",
                turn_id="turn-1",
                usage={"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
                thread_token_usage={
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "total_tokens": 10,
                },
            ),
            codex(
                170,
                "event_msg",
                type="token_count",
                info={
                    "last_token_usage": {"total_tokens": 10},
                    "total_token_usage": {
                        "total_tokens": 10,
                        "output_tokens": 2,
                        "input_tokens": 8,
                    },
                },
            ),
            codex(172, "event_msg", type="task_complete", turn_id="turn-1"),
        ]
    )
    return records


def args(**overrides):
    return argparse.Namespace(
        **{
            "source": "auto",
            "file": None,
            "session": None,
            "project": None,
            **overrides,
        }
    )


def load(arguments):
    trace = select_trace(arguments)
    pair_tools(trace)
    return trace, diagnostic_report(trace)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def test_claude_diagnostics_and_delegated_tool_result(self):
        def event(seconds, kind, **fields):
            return {"timestamp": EPOCH + seconds, "type": kind, **fields}

        records = [
            event(0, "user", message={"content": "作業依頼"}),
            event(
                1,
                "assistant",
                message={
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read",
                            "name": "Read",
                            "input": {
                                "file_path": "README.md",
                                "offset": 5,
                                "limit": 10,
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "agent",
                            "name": "Agent",
                            "input": {
                                "description": "文書の確認",
                                "subagent_type": "explorer",
                                "prompt": "文書を確認して結果を教えて",
                            },
                        },
                    ]
                },
            ),
            event(
                2,
                "user",
                message={
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "agent",
                            "content": "完了",
                        },
                    ]
                },
                toolUseResult={
                    "agentId": "child-a",
                    "status": "completed",
                    "totalDurationMs": 920,
                    "content": [{"type": "text", "text": "確認済み。"}],
                },
            ),
            event(
                3,
                "assistant",
                isApiErrorMessage=True,
                apiErrorStatus=429,
                message={
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "Rate limited"}],
                },
            ),
            event(4, "system", subtype="compact_boundary"),
            event(5, "system", subtype="turn_duration", durationMs=5000),
            event(
                6,
                "user",
                message={
                    "content": (
                        "<task-notification>\n<task-id>child-a</task-id>\n"
                        "<tool-use-id>agent</tool-use-id>\n<status>completed</status>\n"
                        "<summary>追加確認</summary>\n<result>追加の返答</result>\n</task-notification>"
                    )
                },
            ),
        ]
        trace, report = load(
            args(file=write_jsonl(self.root / "claude.jsonl", records))
        )
        events = public_events(trace, False, operations=True, messages=True)
        self.assertEqual(report["counts"]["errors"], 1)
        parsed = {entry["label"]: entry for entry in events}
        error = parsed["モデルAPIエラー"]
        self.assertEqual(
            (error["api_status"], error["operation"]["error_output"]),
            (429, "Rate limited"),
        )
        self.assertEqual(
            parsed["Read"]["operation"]["read_range"], {"offset": 5, "limit": 10}
        )
        delegated = parsed["Agent"]["agent_activity"]
        self.assertEqual(
            (delegated["id"], delegated["description"], delegated["duration_ms"]),
            ("child-a", "文書の確認", 920),
        )
        self.assertEqual(parsed["計測されたターン"]["start"], EPOCH)
        self.assertEqual(parsed["コンテキストを圧縮"]["kind"], "compaction")
        self.assertEqual(parsed["Agent"]["agent_request"], "文書を確認して結果を教えて")
        self.assertEqual(parsed["Agent"]["agent_reply"], "確認済み。")
        self.assertEqual(
            parsed["サブエージェントからの通知"]["agent_reply"], "追加の返答"
        )
        self.assertNotIn(
            "追加の返答", json.dumps(public_events(trace, False), ensure_ascii=False)
        )

    def test_codex_subagent_messages_merge_with_matching_activity_and_keep_sender(self):
        def record(seconds, kind, payload, **fields):
            return {
                "timestamp": EPOCH + seconds,
                "type": kind,
                "payload": payload,
                **fields,
            }

        envelope = (
            "[3] assistant:agent\nMessage Type: FINAL_ANSWER\n"
            "Task name: /root\nSender: /root/reader\nPayload:\n確認完了"
        )
        records = [
            record(0, "session_meta", {"id": "sample"}),
            record(1, "event_msg", {"type": "task_started", "turn_id": "turn"}),
            record(
                2,
                "response_item",
                {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "send_message",
                    "call_id": "call",
                    "arguments": json.dumps(
                        {"target": "reader", "message": "読んでください"}
                    ),
                },
            ),
            record(
                3,
                "event_msg",
                {
                    "type": "item_completed",
                    "turn_id": "turn",
                    "item": {
                        "type": "SubAgentActivity",
                        "kind": "interacted",
                        "agent_path": "/root/reader",
                        "agent_thread_id": "child",
                    },
                },
            ),
            record(
                4,
                "response_item",
                {"type": "function_call_output", "call_id": "call", "output": "sent"},
            ),
            record(
                5,
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": envelope}]},
                metadata={"client_authored": False},
            ),
            record(
                6,
                "response_item",
                {"type": "message", "role": "user", "content": [{"text": envelope}]},
                metadata={"client_authored": True},
            ),
        ]
        trace, _ = load(args(file=write_jsonl(self.root / "codex.jsonl", records)))
        events = public_events(trace, False, messages=True, operations=True)
        sent = next(event for event in events if event.get("agent_request"))
        self.assertEqual(sent["agent_request"], "読んでください")
        self.assertEqual(sent["agent_activity"]["id"], "child")
        self.assertEqual(sent["refs"], [{"line": 3}, {"line": 4}, {"line": 5}])
        incoming = [event for event in events if event["kind"] == "agent"]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["agent_reply"], "確認完了")
        self.assertEqual(incoming[0]["agent_activity"]["name"], "/root/reader")
        user = next(event for event in events if event["kind"] == "user")
        self.assertEqual(user["message"], envelope)

    def test_codex_records_mcp_status_turn_wait_compaction_and_agents(self):
        def event(seconds, kind, **payload):
            return {"timestamp": EPOCH + seconds, "type": kind, "payload": payload}

        def item(seconds, started, item_type, **fields):
            return event(
                seconds,
                "event_msg",
                type="item_completed",
                turn_id="turn",
                started_at_ms=(EPOCH + started) * 1000,
                completed_at_ms=(EPOCH + seconds) * 1000,
                item={"type": item_type, **fields},
            )

        records = [
            event(0, "session_meta", id="codex"),
            event(1, "event_msg", type="task_started", turn_id="turn"),
            item(
                4,
                2,
                "McpToolCall",
                server="demo",
                tool="fetch",
                status="failed",
                result={
                    "isError": True,
                    "content": [{"type": "text", "text": "Service unavailable"}],
                },
            ),
            item(5, 5, "SubAgentActivity", kind="started", agent_thread_id="agent"),
            event(6, "compacted"),
            item(6, 5, "ContextCompaction"),
            item(8, 8, "SubAgentActivity", kind="completed", agent_thread_id="agent"),
            event(
                9,
                "event_msg",
                type="task_complete",
                turn_id="turn",
                time_to_first_token_ms=725,
            ),
            event(10, "event_msg", type="task_started", turn_id="aborted"),
            event(
                11,
                "event_msg",
                type="turn_aborted",
                turn_id="aborted",
                reason="interrupted",
            ),
        ]
        trace, report = load(args(file=write_jsonl(self.root / "codex.jsonl", records)))
        events = public_events(trace, False, operations=True)
        self.assertEqual(report["counts"]["errors"], 1)
        turns = [event for event in events if event["kind"] == "turn"]
        self.assertEqual(turns[0]["first_token_ms"], 725)
        self.assertTrue(turns[1]["interrupted"])
        self.assertEqual(turns[1]["reason"], "interrupted")
        mcp = next(event for event in events if event.get("server"))
        self.assertEqual(mcp["operation"]["error_output"], "Service unavailable")
        compactions = [event for event in events if event["kind"] == "compaction"]
        self.assertEqual(len(compactions), 1)
        self.assertEqual(len(compactions[0]["refs"]), 2)
        agent = next(event for event in events if event["kind"] == "agent")
        self.assertEqual(agent["agent_activity"]["status"], "completed")
        self.assertEqual(agent["end"] - agent["start"], 3)

    def test_claude_slash_command_displays_name_and_preserves_markup_in_arguments(self):
        wrapper = (
            "<command-message>aidlc</command-message>\n"
            "<command-name>/aidlc</command-name>\n"
            "<command-args>割り勘計算アプリ\n<feature>ブラウザのみ</feature></command-args>"
        )
        reverse_order = (
            "<command-name>/clear</command-name>\n"
            "<command-message>clear</command-message>"
        )
        plain = "XMLの例\n<command-name>/clear</command-name>"
        records = [
            {
                "type": "user",
                "timestamp": EPOCH + index,
                "message": {"role": "user", "content": content},
            }
            for index, content in enumerate((wrapper, reverse_order, plain))
        ]
        trace, _ = load(args(file=write_jsonl(self.root / "claude.jsonl", records)))
        events = public_events(trace, False, messages=True)
        self.assertEqual(
            [event["message"] for event in events],
            [
                "/aidlc\n割り勘計算アプリ\n<feature>ブラウザのみ</feature>",
                "/clear",
                plain,
            ],
        )
        self.assertEqual(trace.events[0]["content"], wrapper)
        self.assertEqual(events[0]["refs"], [{"line": 1}])

    def test_file_operations_export_targets_and_captured_changes(self):
        records = [
            {
                "type": "session_meta",
                "timestamp": EPOCH,
                "payload": {"id": "files", "cwd": str(self.root)},
            },
            *[
                {
                    "type": "response_item",
                    "timestamp": EPOCH + index,
                    "payload": {
                        "type": "function_call",
                        "name": name,
                        "call_id": name,
                        "arguments": json.dumps({"file_path": f"{name.lower()}.txt"}),
                    },
                }
                for index, name in enumerate(("Read", "Write", "Edit"), 1)
            ],
            {
                "type": "event_msg",
                "timestamp": EPOCH + 5,
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "status": "completed",
                        "changes": {
                            "added.txt": {"type": "add", "content": PRIVATE},
                            "before.txt": {"type": "update", "move_path": "after.txt"},
                            "deleted.txt": {"type": "delete"},
                        },
                    },
                },
            },
        ]
        trace, _ = load(args(file=write_jsonl(self.root / "files.jsonl", records)))
        events = public_events(trace, False, operations=True)
        for tool, expected in zip(events[:3], ("read.txt", "write.txt", "edit.txt")):
            self.assertEqual(tool["operation"]["paths"], [expected])
            self.assertTrue(tool["operation"]["file_operation"])
        self.assertEqual(
            events[3]["operation"]["changes"],
            [
                {"path": "added.txt", "action": "add"},
                {"path": "before.txt", "action": "update", "destination": "after.txt"},
                {"path": "deleted.txt", "action": "delete"},
            ],
        )
        self.assertNotIn(PRIVATE, json.dumps(events))
        self.assertIn("BODY-DO-NOT-EXPORT", json.dumps(public_events(trace, True)))
        self.assertTrue(
            all("operation" not in event for event in public_events(trace, False))
        )

    def test_parallel_calls_repetition_and_private_output(self):
        path = write_jsonl(self.root / "codex.jsonl", codex_records(self.root))
        with path.open("a", encoding="utf-8") as log:
            log.write('{"interrupted":')
        trace, report = load(args(file=path))
        self.assertEqual(report["range"]["elapsed"], 172)
        self.assertEqual(report["warnings"], {"invalid_record": 1, "unmatched_tool": 1})
        self.assertEqual(report["counts"]["errors"], 3)
        self.assertEqual(report["counts"]["repeats"], 1)
        repeat = next(
            finding for finding in report["findings"] if finding["kind"] == "repeat"
        )
        self.assertEqual(
            (repeat["period"], repeat["count"], len(repeat["event_ids"])), (2, 3, 6)
        )
        self.assertEqual(report["longest"][0]["seconds"], 12)
        gap = next(
            finding for finding in report["findings"] if finding["kind"] == "gap"
        )
        self.assertEqual(gap["seconds"], 139)
        self.assertEqual(len(gap["event_ids"]), 2)
        self.assertEqual(report["cost_summary"]["status"], "unknown")
        self.assertEqual(report["token_summary"]["values"]["total"], 10)
        self.assertEqual([item["value"] for item in report["usage_highlights"]], [10])
        events = public_events(trace, False)
        model = next(event for event in events if event["kind"] == "model")
        self.assertEqual(model["token_count"], 10)
        read, check = [event for event in events if event["kind"] == "tool"][:2]
        self.assertNotIn("token_count", read)
        self.assertEqual((read["start"], read["end"]), (EPOCH + 2, EPOCH + 14))
        self.assertEqual((check["start"], check["end"]), (EPOCH + 3, EPOCH + 5))
        self.assertEqual(len(read["refs"]), 2)
        self.assertNotIn("BODY-DO-NOT-EXPORT", render_html(report, events, False))
        messages = public_events(trace, False, messages=True)
        user = next(event for event in messages if event["kind"] == "user")
        self.assertEqual(user["message"], PRIVATE)
        self.assertNotIn("content", user)
        self.assertNotIn("</ScRiPt>", render_html(report, messages, False))
        self.assertNotIn(
            "BODY-DO-NOT-EXPORT", json.dumps(diagnostic_events(events, report))
        )
        full = render_html(report, public_events(trace, True), True)
        self.assertIn("BODY-DO-NOT-EXPORT", full)
        self.assertNotIn("</ScRiPt>", full)

    def test_project_boundary_parent_and_exact_session(self):
        root = self.root / "repo"
        workspace = root / "app"
        workspace.mkdir(parents=True)
        home = self.root / "codex"
        sessions = home / "sessions"
        for sid, cwd, delta, child in [
            ("parent-old", root, 0, False),
            ("parent", workspace, 20, False),
            ("child", root, 30, True),
            ("neighbor", self.root / "repo-copy", 40, False),
        ]:
            records = codex_records(cwd)[:4]
            records[0]["payload"]["id"] = sid
            if child:
                records[0]["payload"]["source"] = {"subagent": {}}
            for record in records:
                record["timestamp"] += delta
            write_jsonl(sessions / f"rollout-{sid}.jsonl", records)
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(
                select_trace(args(source="codex", project=root)).session.id, "parent"
            )
            self.assertEqual(
                select_trace(args(source="codex", session="child")).session.id, "child"
            )
            with self.assertRaisesRegex(ValueError, "対象セッションが見つかりません"):
                select_trace(args(source="codex", session="paren"))
            write_jsonl(home / "archived_sessions/rollout-duplicate.jsonl", records)
            with self.assertRaisesRegex(ValueError, "複数"):
                select_trace(args(source="codex", session="neighbor"))

    def test_claude_usage_dedup_and_cost_snapshot(self):
        usage = {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 7}
        records = [
            {
                "type": "user",
                "sessionId": "claude",
                "timestamp": EPOCH,
                "message": {"content": "check"},
            },
            {
                "type": "assistant",
                "timestamp": EPOCH + 1,
                "message": {
                    "id": "m",
                    "model": "demo",
                    "usage": usage,
                    "content": [{"type": "thinking", "thinking": PRIVATE}],
                },
            },
            {
                "type": "assistant",
                "timestamp": EPOCH + 2,
                "message": {
                    "id": "m",
                    "model": "demo",
                    "usage": usage,
                    "content": [{"type": "text", "text": "done"}],
                },
            },
            {"type": "cost-state", "sessionId": "claude", "totalCostUSD": 20},
            {
                "type": "cost-state",
                "sessionId": "claude",
                "totalCostUSD": 0,
                "hasUnknownModelCost": True,
            },
        ]
        trace, report = load(
            args(file=write_jsonl(self.root / "claude.jsonl", records))
        )
        self.assertEqual(
            report["token_summary"]["values"],
            {"input": 10, "output": 3, "cache_read": 7},
        )
        self.assertEqual(report["cost_summary"]["status"], "partial")
        self.assertEqual(report["cost_summary"]["value"], 0)
        self.assertEqual(len([e for e in trace.events if e["kind"] == "model"]), 1)
        model = next(
            event for event in public_events(trace, False) if event["kind"] == "model"
        )
        self.assertEqual(model["token_count"], 20)

    def test_opencode_export_and_read_only_sqlite(self):
        msg = {
            "id": "message",
            "role": "assistant",
            "parentID": "turn",
            "modelID": "demo",
            "time": {"created": EPOCH, "completed": EPOCH + 20},
            "cost": 9,
            "tokens": {"input": 400},
        }
        parts = [
            {"type": "text", "text": PRIVATE},
            {
                "type": "tool",
                "tool": "list",
                "callID": "tool",
                "state": {
                    "status": "completed",
                    "input": {},
                    "output": "ok",
                    "time": {"start": EPOCH + 2, "end": EPOCH + 4},
                },
            },
            {"type": "step-finish", "cost": 0, "tokens": {"input": 14}},
            {"type": "step-finish", "tokens": {"input": 28}},
        ]
        export = self.root / "export.json"
        export.write_text(
            json.dumps(
                {
                    "info": {"id": "session"},
                    "messages": [{"info": msg, "parts": parts}],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        db = self.root / "logs #.db"
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE session (id TEXT, time_updated INTEGER, directory TEXT, parent_id TEXT);
                CREATE TABLE message (id TEXT, time_created INTEGER, session_id TEXT, data TEXT);
                CREATE TABLE part (id TEXT, time_created INTEGER, message_id TEXT, data TEXT);
            """)
            conn.executemany(
                "INSERT INTO session VALUES (?, ?, ?, ?)",
                [
                    ("session", EPOCH + 40, str(self.root), None),
                    ("other", EPOCH + 80, str(self.root), None),
                ],
            )
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                ("message", EPOCH, "session", json.dumps(msg)),
            )
            conn.executemany(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                [
                    (str(i), EPOCH + i, "message", json.dumps(p))
                    for i, p in enumerate(parts)
                ],
            )
        before = db.read_bytes()
        for path in (export, db):
            with self.subTest(path=path.name):
                trace, report = load(args(file=path, session="session"))
                self.assertEqual(report["cost_summary"]["status"], "partial")
                self.assertEqual(report["cost_summary"]["value"], 0)
                self.assertEqual(report["token_summary"]["values"]["input"], 42)
                tool = next(event for event in trace.events if event["kind"] == "tool")
                self.assertEqual(tool["end"] - tool["start"], 2)
                ref = tool["refs"][0]
                self.assertEqual(
                    ref,
                    {"pointer": "/messages/0/parts/1"}
                    if path == export
                    else {"table": "part", "id": "1"},
                )
        self.assertEqual(db.read_bytes(), before)

    def test_changed_result_and_turn_boundaries_prevent_loop_candidate(self):
        records = codex_records(self.root)[:4]
        cases = [
            (2, "a", "same"),
            (4, "b", "different"),
            (6, "c", "same"),
            (8, "d", "same"),
            (12, "e", "same"),
        ]
        for t, id_, value in cases:
            if t == 12:
                records.append(
                    {
                        "type": "event_msg",
                        "timestamp": EPOCH + 10,
                        "payload": {"type": "task_started", "turn_id": "turn-2"},
                    }
                )
            records.extend(
                [
                    {
                        "type": "response_item",
                        "timestamp": EPOCH + t,
                        "payload": {
                            "type": "function_call",
                            "name": "poll",
                            "call_id": id_,
                            "arguments": "{}",
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": EPOCH + t + 1,
                        "payload": {
                            "type": "function_call_output",
                            "call_id": id_,
                            "output": value,
                        },
                    },
                ]
            )
        _, report = load(args(file=write_jsonl(self.root / "turns.jsonl", records)))
        self.assertEqual(report["counts"]["repeats"], 0)


if __name__ == "__main__":
    unittest.main()
