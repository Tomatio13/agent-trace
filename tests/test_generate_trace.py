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
