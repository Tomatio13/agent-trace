#!/usr/bin/env python3
"""Build an offline execution timeline from local agent session logs."""

import argparse
import html
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import webbrowser
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path


AGENTS = {"claude": "Claude Code", "codex": "Codex CLI", "opencode": "OpenCode"}
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SLOW_SECONDS = 10
GAP_SECONDS = 30
REPEAT_COUNT = 3


def number(value):
    if type(value) in (int, float) and 0 <= value <= sys.float_info.max:
        return value
    return None


def timestamp(value):
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    value = number(value)
    if value:
        seconds = value / 1000 if value >= 1e11 else value
        if seconds <= 8.64e12:
            return seconds
    return None


def obj(value):
    return value if isinstance(value, dict) else {}


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def normalized_path(value):
    if isinstance(value, str) and Path(value).expanduser().is_absolute():
        return Path(value).expanduser().resolve()
    return None


def project_matches(project, directory):
    path = normalized_path(directory)
    return path is not None and path.is_relative_to(project)


@dataclass
class Session:
    source: str
    path: Path
    id: str
    cwd: str | None
    updated: float
    parent: bool = False
    parent_id: str | None = None
    history_start_ordinal: int | None = None


@dataclass
class Trace:
    session: Session
    events: list = field(default_factory=list)
    warnings: dict = field(default_factory=lambda: defaultdict(int))
    snapshots: dict = field(default_factory=dict)

    def add(self, kind, label, at, ref, *, end=None, timing=None, **fields):
        if end is not None and (at is None or end < at):
            self.warnings["invalid_interval"] += 1
            end = None
        event = {
            "id": len(self.events) + 1,
            "kind": kind,
            "label": label,
            "start": at,
            "end": end,
            "timing": timing if end is not None else None,
            "refs": [ref],
            **fields,
        }
        self.events.append(event)
        return event


def jsonl_records(lines, warnings=None):
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                yield line_no, record
                continue
        except ValueError:
            pass
        if warnings is not None:
            warnings["invalid_record"] += 1


def jsonl_header(path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        return [record for _, record in jsonl_records(islice(stream, 256))]


def jsonl_recent(path, records):
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - 262144))
        if size > 262144:
            stream.readline()
        tail = (record for _, record in jsonl_records(stream.read().splitlines()))
        times = [timestamp(record.get("timestamp")) for record in (*records, *tail)]
    return max(
        (value for value in times if value is not None), default=path.stat().st_mtime
    )


def identify_jsonl(records):
    for record in records:
        kind = record.get("type")
        if kind == "session_meta" or kind in (
            "response_item",
            "event_msg",
            "turn_context",
        ):
            return "codex"
        if record.get("sessionId") or (
            kind in ("user", "assistant") and "message" in record
        ):
            return "claude"
        if record.get("sessionID") or obj(record.get("part")).get("sessionID"):
            return "opencode"
    return None


def jsonl_session(path, source, records=None):
    if records is None:
        records = jsonl_header(path)
    parent_id = None
    history_start = None
    if source == "codex":
        meta = next(
            (obj(r.get("payload")) for r in records if r.get("type") == "session_meta"),
            {},
        )
        sid = meta.get("id") or meta.get("session_id")
        cwd = meta.get("cwd")
        origin = meta.get("source")
        child_source = obj(origin).get("subagent")
        thread_spawn = obj(obj(child_source).get("thread_spawn"))
        parent_id = meta.get("parent_thread_id") or thread_spawn.get("parent_thread_id")
        child = child_source is not None or bool(parent_id)
        ordinal = meta.get("subagent_history_start_ordinal")
        if type(ordinal) is int and ordinal >= 0:
            history_start = ordinal
    elif source == "claude":
        sid = next(
            (
                r.get("sessionId") or r.get("session_id")
                for r in records
                if r.get("sessionId") or r.get("session_id")
            ),
            None,
        )
        cwd = next((r["cwd"] for r in records if r.get("cwd")), None)
        first_message = next(
            (r for r in records if r.get("type") in ("user", "assistant")), {}
        )
        child = "subagents" in path.parts or bool(first_message.get("isSidechain"))
        if "subagents" in path.parts:
            sid = path.stem
            if path.parent.name == "subagents":
                parent_id = path.parent.parent.name
    else:
        sid = next(
            (
                r.get("sessionID") or obj(r.get("part")).get("sessionID")
                for r in records
                if r.get("sessionID") or obj(r.get("part")).get("sessionID")
            ),
            None,
        )
        cwd = None
        child = False
    return Session(
        source, path, sid or path.stem, cwd, 0, child, parent_id, history_start
    )


def open_db(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def db_sessions(path, sid=None):
    with closing(open_db(path)) as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(session)")
        }
        if not {"id", "time_updated"} <= columns:
            raise ValueError("OpenCode SQLite の session スキーマに対応していません")
        directory = "directory" if "directory" in columns else "NULL"
        parent = "parent_id" if "parent_id" in columns else "NULL"
        query = f"SELECT id, time_updated, {directory}, {parent} FROM session"
        rows = (
            connection.execute(query + " WHERE id = ?", (sid,))
            if sid
            else connection.execute(query)
        )
        return [
            Session(
                "opencode", path, id_, cwd, timestamp(updated) or 0, bool(parent_id)
            )
            for id_, updated, cwd, parent_id in rows
        ]


def opencode_export(path):
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        try:
            data = json.load(stream)
        except ValueError:
            return None
    if isinstance(obj(data).get("messages"), list):
        return data
    return None


def explicit_sessions(path, source, sid):
    if not path.is_file():
        raise ValueError(f"ログファイルが見つかりません: {path}")
    if path.suffix.lower() in DB_SUFFIXES:
        detected = "opencode"
        candidates = db_sessions(path, sid) if source in ("auto", detected) else []
    elif (
        path.suffix.lower() == ".json" and (export := opencode_export(path)) is not None
    ):
        detected = "opencode"
        info = obj(export.get("info"))
        candidates = [
            Session(
                detected, path, info.get("id") or path.stem, info.get("directory"), 0
            )
        ]
    else:
        records = jsonl_header(path)
        detected = identify_jsonl(records)
        if not detected:
            raise ValueError(
                "ログ形式を識別できません。対応する JSONL、OpenCode JSON エクスポート、または SQLite を指定してください"
            )
        candidates = [jsonl_session(path, detected, records)]
    if source not in ("auto", detected):
        raise ValueError(
            f"指定された --source {source} とログ形式 {detected} が一致しません"
        )
    return candidates


def discover(source, project, sid, *, read_updated=True):
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
    claude_home = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"
    ).expanduser()
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or home / ".local/share"
    ).expanduser()
    roots = [
        ("codex", codex_home / "sessions", "rollout-*.jsonl"),
        ("codex", codex_home / "archived_sessions", "rollout-*.jsonl"),
        ("claude", claude_home / "projects", "*.jsonl"),
    ]
    candidates = []
    inaccessible = 0
    for kind, root, pattern in roots:
        if source not in ("auto", kind):
            continue
        for path in root.rglob(pattern):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                records = jsonl_header(path)
                if identify_jsonl(records) != kind:
                    continue
                session = jsonl_session(path, kind, records)
                if sid is not None and session.id != sid:
                    continue
                if project is not None and not project_matches(project, session.cwd):
                    continue
                if read_updated:
                    session.updated = jsonl_recent(path, records)
                candidates.append(session)
            except OSError:
                inaccessible += 1

    db = (data_home / "opencode/opencode.db").resolve()
    if source in ("auto", "opencode") and db.is_file():
        try:
            candidates.extend(db_sessions(db, sid))
        except (sqlite3.Error, ValueError):
            if source == "opencode":
                raise
            inaccessible += 1
    return candidates, inaccessible


def session_candidates(args):
    project = (
        normalized_path(str(Path(args.project).expanduser().resolve()))
        if args.project
        else None
    )
    if project is not None and not project.is_dir():
        raise ValueError(f"プロジェクトのディレクトリが見つかりません: {project}")
    if args.file:
        candidates = explicit_sessions(
            Path(args.file).expanduser().resolve(), args.source, args.session
        )
        inaccessible = 0
    else:
        candidates, inaccessible = discover(args.source, project, args.session)
    if project is not None:
        candidates = [s for s in candidates if project_matches(project, s.cwd)]
    if args.session:
        candidates = [s for s in candidates if s.id == args.session]
    if not candidates:
        target = args.session or args.project or args.source
        note = f"（読み込めなかったログ {inaccessible} 件）" if inaccessible else ""
        raise ValueError(
            f"対象セッションが見つかりません: {target}{note}。--source、--session、または --file を確認してください"
        )
    if args.session and len(candidates) > 1:
        matches = "\n".join(f"  {s.source} {s.id} {s.path}" for s in candidates)
        raise ValueError(
            f"セッションIDに複数のログが一致しました。--file で指定してください:\n{matches}"
        )
    return sorted(
        candidates,
        key=lambda s: (
            not (project and s.parent),
            s.updated,
            s.source,
            str(s.path),
            s.id,
        ),
        reverse=True,
    ), inaccessible


class EmptyTrace(ValueError):
    pass


def select_trace(args):
    candidates, inaccessible = session_candidates(args)
    for skipped, session in enumerate(candidates):
        try:
            trace = read_trace(session, inaccessible)
        except EmptyTrace:
            if args.file or args.session:
                raise
            continue
        if skipped:
            trace.warnings["empty_sessions_skipped"] = skipped
        return trace
    raise EmptyTrace(
        f"一致した {len(candidates)} 件のセッションに表示できるイベントがありません"
    )


def usage_tokens(data, names):
    return {
        label: value
        for label, key in names.items()
        if (value := number(obj(data).get(key))) is not None
    }


CLAUDE_TOKENS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}
CODEX_TOKENS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cached_input_tokens",
    "cache_write": "cache_write_input_tokens",
    "reasoning": "reasoning_output_tokens",
    "total": "total_tokens",
}


def claude_trace(trace, records):
    generations = {}
    turn = None
    agent_calls = set()
    agent_ids = set()
    for line, record in records:
        ref = {"line": line}
        at = timestamp(record.get("timestamp"))
        kind = record.get("type")
        msg = obj(record.get("message"))
        if kind == "cost-state":
            cost = number(record.get("totalCostUSD"))
            model_costs = {
                model: value
                for model, stats in obj(record.get("modelUsage")).items()
                if (value := number(obj(stats).get("costUSD"))) is not None
            }
            trace.snapshots["cost"] = {
                "value": cost,
                "ref": ref,
                "basis": "Claude Code · cost-state",
                "partial": bool(record.get("hasUnknownModelCost")),
                "models": model_costs,
            }
        if kind not in ("user", "assistant"):
            subtype = record.get("subtype")
            duration_ms = number(record.get("durationMs"))
            if (
                kind == "system"
                and subtype == "turn_duration"
                and at is not None
                and duration_ms is not None
            ):
                trace.add(
                    "turn",
                    "計測されたターン",
                    at - duration_ms / 1000,
                    ref,
                    end=at,
                    timing="turn",
                    turn=turn,
                )
            elif kind == "system" and subtype == "compact_boundary":
                trace.add("compaction", "コンテキストを圧縮", at, ref, turn=turn)
            elif kind == "system" and record.get("level") == "error":
                trace.add(
                    "error",
                    "システムエラー",
                    at,
                    ref,
                    turn=turn,
                    content=record.get("content"),
                )
            continue
        blocks = msg.get("content", record.get("content", []))
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if kind == "assistant" and record.get("isApiErrorMessage"):
            error_text = "\n".join(
                block["text"]
                for block in (
                    obj(block) for block in (blocks if isinstance(blocks, list) else [])
                )
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            )
            trace.add(
                "error",
                "モデルAPIエラー",
                at,
                ref,
                turn=turn,
                api_status=number(record.get("apiErrorStatus")),
                reason=record.get("error"),
                content=error_text or record.get("error"),
            )
            continue
        if not isinstance(blocks, list):
            continue
        if kind == "user":
            remaining = []
            for block in blocks:
                text = obj(block).get("text")
                notice = claude_agent_notice(text, agent_calls, agent_ids)
                if notice:
                    activity, reply = notice
                    trace.add(
                        "agent",
                        "サブエージェントからの通知",
                        at,
                        ref,
                        turn=turn,
                        agent_activity=activity,
                        agent_reply=reply,
                    )
                else:
                    remaining.append(block)
            blocks = remaining
        if (
            kind == "user"
            and any(obj(b).get("type") == "text" for b in blocks)
            and not any(obj(b).get("type") == "tool_result" for b in blocks)
            and not record.get("isMeta")
            and not record.get("isSidechain")
        ):
            turn = record.get("uuid") or f"line-{line}"
        usage = usage_tokens(msg.get("usage"), CLAUDE_TOKENS)
        if (
            kind == "assistant"
            and msg.get("model") != "<synthetic>"
            and (usage or msg.get("id"))
        ):
            key = msg.get("id")
            model = msg.get("model")
            event = generations.get(key) if key else None
            if event is None:
                event = trace.add(
                    "model", "モデル応答", at, ref, turn=turn, model=model, tokens=usage
                )
                if key:
                    generations[key] = event
            elif usage:
                event["tokens"].update(usage)
                event["refs"].append(ref)
            if msg.get("stop_reason"):
                event["stop_reason"] = msg["stop_reason"]
        for block in blocks:
            block = obj(block)
            block_type = block.get("type")
            if block_type == "text":
                label = "ユーザー入力" if kind == "user" else "応答"
                trace.add(
                    kind,
                    label,
                    at,
                    ref,
                    turn=turn,
                    content=block.get("text"),
                    synthetic=bool(record.get("isMeta"))
                    or (kind == "user" and bool(record.get("isSidechain"))),
                )
            elif block_type == "thinking":
                trace.add("reasoning", "推論イベント", at, ref, turn=turn)
            elif block_type == "tool_use":
                name = block.get("name") or "ツール"
                inputs = block.get("input")
                is_agent = name.lower() in ("agent", "task")
                if is_agent and isinstance(block.get("id"), str):
                    agent_calls.add(block["id"])
                trace.add(
                    "tool",
                    name,
                    at,
                    ref,
                    turn=turn,
                    call=block.get("id"),
                    content=inputs,
                    agent_activity=claude_agent_details(obj(inputs))
                    if is_agent
                    else None,
                    agent_request=plain_text(obj(inputs).get("prompt"))
                    if is_agent
                    else None,
                )
            elif block_type == "tool_result":
                failed = bool(block.get("is_error"))
                result = obj(record.get("toolUseResult"))
                agent_id = result.get("agentId")
                if isinstance(agent_id, str):
                    agent_ids.add(agent_id)
                trace.add(
                    "result",
                    "ツール結果",
                    at,
                    ref,
                    turn=turn,
                    call=block.get("tool_use_id"),
                    failed=failed,
                    content=block.get("content"),
                    answers=result.get("answers"),
                    agent_activity=claude_agent_details(result, result=True),
                    agent_reply=plain_text(
                        result.get("content") or block.get("content")
                    )
                    if agent_id and result.get("status") == "completed" and not failed
                    else None,
                )


def plain_text(content):
    if isinstance(content, list):
        content = "\n".join(
            block["text"]
            for block in (obj(block) for block in content)
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return content if isinstance(content, str) and content.strip() else None


def claude_agent_notice(text, calls, agent_ids):
    if not isinstance(text, str):
        return None
    envelope = re.fullmatch(
        r"\s*<task-notification>(.*)</task-notification>\s*", text, re.DOTALL
    )
    if not envelope:
        return None

    def field(name):
        match = re.search(rf"<{name}>(.*?)</{name}>", envelope[1], re.DOTALL)
        return match[1].strip() if match else None

    identifier = field("task-id")
    if field("tool-use-id") not in calls and identifier not in agent_ids:
        return None
    return {
        "id": identifier,
        "status": field("status"),
        "description": field("summary"),
    }, field("result")


def codex_agent_reply(text):
    envelope = re.fullmatch(
        r"\s*(?:\[\d+\] assistant:[^\r\n]+\s*\n)?"
        r"Message Type: (?P<type>MESSAGE|FINAL_ANSWER)\s*\n"
        r"Task name: (?P<target>/\S+)\s*\nSender: (?P<sender>/\S+)\s*\n"
        r"Payload:\s*\n(?P<text>.+)",
        text,
        re.DOTALL,
    )
    if not envelope:
        return None
    return {
        "name": envelope["sender"],
        "recipient": envelope["target"],
        "status": "completed" if envelope["type"] == "FINAL_ANSWER" else "interacted",
    }, envelope["text"]


def claude_agent_details(info, *, result=False):
    if result and not info.get("agentId"):
        return None
    details = {
        "id": info.get("agentId"),
        "name": info.get("name"),
        "type": info.get("subagent_type")
        or info.get("agentType")
        or info.get("agent_type"),
        "description": info.get("description"),
        "status": info.get("status"),
        "model": info.get("resolvedModel"),
        "background": info.get("run_in_background"),
        "duration_ms": number(info.get("totalDurationMs")),
        "tokens": number(info.get("totalTokens")),
    }
    if result and info.get("isAsync") is True:
        details["background"] = True
    return {key: value for key, value in details.items() if value is not None}


def codex_agent_event(trace, item, turn, ref, started, completed, at, active_agents):
    if item["type"] == "CollabAgentToolCall":
        operation = item.get("tool")
        raw_targets = item.get("receiver_thread_ids")
        targets = (
            [target for target in raw_targets if isinstance(target, str)]
            if isinstance(raw_targets, list)
            else []
        )
        statuses = {}
        for target, state in obj(item.get("agents_states")).items():
            status = obj(state).get("status")
            if isinstance(status, str):
                statuses[target] = status
        trace.add(
            "agent",
            "サブエージェントの完了待ち"
            if operation == "wait"
            else "サブエージェントを操作",
            started or completed or at,
            ref,
            end=completed if started else None,
            timing="recorded",
            turn=turn,
            failed=item.get("status") == "failed",
            agent_activity={
                "action": operation,
                "status": item.get("status"),
                "targets": targets,
                "states": statuses,
            },
        )
        return
    agent_id = item.get("agent_thread_id")
    phase = item.get("kind")
    activity = {
        "id": agent_id,
        "name": item.get("agent_path"),
        "status": phase,
        "model": item.get("model"),
        "effort": item.get("reasoning_effort"),
    }
    if phase == "completed" and isinstance(agent_id, str) and agent_id in active_agents:
        previous = active_agents.pop(agent_id)
        end = completed or at
        if (
            previous["start"] is not None
            and end is not None
            and end >= previous["start"]
        ):
            previous.update(end=end, timing="recorded", label="サブエージェントの実行")
            previous["agent_activity"].update(
                {k: v for k, v in activity.items() if v is not None}
            )
            previous["refs"].append(ref)
            return
    labels = {
        "started": "サブエージェントを開始",
        "completed": "サブエージェントが完了",
        "interacted": "サブエージェントとのやり取り",
    }
    event = trace.add(
        "agent",
        labels.get(phase, "サブエージェントの活動"),
        started or completed or at,
        ref,
        end=completed if started else None,
        timing="recorded",
        turn=turn,
        agent_activity=activity,
    )
    if phase == "started" and isinstance(agent_id, str):
        active_agents[agent_id] = event


def codex_trace(trace, records):
    turns = {}
    model_by_turn = {}
    response_usage = {}
    response_totals = set()
    legacy_usage = []
    legacy_messages = []
    response_messages = set()
    compactions = {}
    active_agents = {}
    pending_agent_sends = {}
    turn = None
    explicit_turn = False
    for line, record in records:
        ref = {"line": line}
        at = timestamp(record.get("timestamp"))
        kind = record.get("type")
        payload = obj(record.get("payload"))
        sub = payload.get("type")
        if kind == "session_meta":
            continue
        if kind == "compacted":
            compactions[turn] = trace.add(
                "compaction",
                "コンテキストを圧縮",
                at,
                ref,
                turn=turn,
            )
        elif kind == "turn_context":
            if key := payload.get("turn_id"):
                turn = key
                explicit_turn = True
                model_by_turn[turn] = payload.get("model")
        elif kind == "token_usage_record":
            usage = usage_tokens(payload.get("usage"), CODEX_TOKENS)
            key = payload.get("response_id")
            if key and usage:
                response_usage[key] = (at, ref, payload.get("turn_id", turn), usage)
            total = usage_tokens(payload.get("thread_token_usage"), CODEX_TOKENS)
            if total:
                trace.snapshots["tokens"] = (total, ref)
                if key and usage:
                    response_totals.add(tuple(sorted(total.items())))
        elif kind == "event_msg":
            key = payload.get("turn_id") or turn
            if sub in ("task_started", "turn_started"):
                turn = payload.get("turn_id") or f"line-{line}"
                explicit_turn = True
                turns[turn] = trace.add(
                    "turn",
                    "ターン",
                    timestamp(payload.get("started_at")) or at,
                    ref,
                    turn=turn,
                )
            elif sub in ("task_complete", "turn_completed", "turn_aborted"):
                event = turns.get(key)
                interrupted = sub == "turn_aborted"
                if event is None and interrupted:
                    event = trace.add(
                        "turn",
                        "ターン",
                        timestamp(payload.get("started_at")) or at,
                        ref,
                        turn=key,
                    )
                if event is not None:
                    event.update(
                        interrupted=interrupted,
                        reason=payload.get("reason") if interrupted else None,
                        first_token_ms=number(payload.get("time_to_first_token_ms")),
                    )
                    end = timestamp(payload.get("completed_at")) or at
                    if (
                        end is not None
                        and event["start"] is not None
                        and end >= event["start"]
                    ):
                        event.update(
                            end=end,
                            timing="turn",
                            failed=False,
                        )
                    if event["refs"][-1] != ref:
                        event["refs"].append(ref)
                if key == turn:
                    turn = None
                    explicit_turn = False
            elif sub == "token_count":
                info = obj(payload.get("info"))
                total = usage_tokens(info.get("total_token_usage"), CODEX_TOKENS)
                if total:
                    trace.snapshots["tokens"] = (total, ref)
                usage = usage_tokens(info.get("last_token_usage"), CODEX_TOKENS)
                if usage:
                    legacy_usage.append((at, ref, key, usage, total))
            elif sub == "item_completed":
                item = obj(payload.get("item"))
                started = timestamp(payload.get("started_at_ms"))
                completed = timestamp(payload.get("completed_at_ms"))
                item_type = item.get("type")
                if item_type == "CommandExecution":
                    exit_code = item.get("exit_code")
                    status = item.get("status")
                    trace.add(
                        "command",
                        "コマンド実行",
                        started or completed or at,
                        ref,
                        end=completed if started else None,
                        timing="recorded",
                        turn=key,
                        command=item.get("command"),
                        cwd=item.get("cwd"),
                        parsed_cmd=item.get("parsed_cmd"),
                        output=item.get("aggregated_output"),
                        exit_code=exit_code,
                        failed=status == "failed"
                        or (type(exit_code) is int and exit_code != 0),
                    )
                elif item_type == "FileChange":
                    trace.add(
                        "file_change",
                        "ファイル変更の記録",
                        started or completed or at,
                        ref,
                        end=completed if started else None,
                        timing="recorded",
                        turn=key,
                        content=item.get("changes"),
                        output=item.get("stderr") or item.get("stdout"),
                        failed=item.get("status") in ("failed", "declined"),
                    )
                elif item_type == "McpToolCall":
                    name = item.get("tool") or "MCPツール"
                    status = item.get("status")
                    result = item.get("result")
                    trace.add(
                        "tool",
                        name,
                        started or completed or at,
                        ref,
                        end=completed if started else None,
                        timing="recorded",
                        turn=key,
                        content=decoded(item.get("arguments")),
                        output=result,
                        server=item.get("server"),
                        status=status,
                        failed=status in ("failed", "declined")
                        or obj(result).get("isError") is True,
                    )
                elif item_type == "ContextCompaction":
                    compaction = compactions.pop(key, None)
                    if compaction is None:
                        trace.add(
                            "compaction",
                            "コンテキストを圧縮",
                            started or completed or at,
                            ref,
                            end=completed if started else None,
                            timing="recorded",
                            turn=key,
                        )
                    else:
                        compaction["refs"].append(ref)
                        if (
                            started is not None
                            and completed is not None
                            and completed >= started
                        ):
                            compaction.update(
                                start=started, end=completed, timing="recorded"
                            )
                elif item_type in ("SubAgentActivity", "CollabAgentToolCall"):
                    if (
                        item_type == "SubAgentActivity"
                        and item.get("kind") == "interacted"
                    ):
                        name = item.get("agent_path")
                        identifier = item.get("agent_thread_id")
                        sends = []
                        for candidate in pending_agent_sends.values():
                            target = candidate["agent_activity"].get("recipient")
                            if not target or candidate.get("turn") != key:
                                continue
                            if target in (name, identifier) or (
                                isinstance(name, str)
                                and not target.startswith("/")
                                and name.endswith("/" + target)
                            ):
                                sends.append(candidate)
                        if len(sends) == 1:
                            sends[0]["agent_activity"].update(
                                name=name, id=identifier, model=item.get("model")
                            )
                            sends[0]["refs"].append(ref)
                            continue
                    codex_agent_event(
                        trace, item, key, ref, started, completed, at, active_agents
                    )
                elif item_type == "Reasoning" and started and completed:
                    trace.add(
                        "reasoning",
                        "記録された推論区間",
                        started,
                        ref,
                        end=completed,
                        timing="recorded",
                        turn=key,
                    )
            elif sub in ("user_message", "agent_message", "agent_reasoning"):
                role = {
                    "user_message": "user",
                    "agent_message": "assistant",
                    "agent_reasoning": "reasoning",
                }[sub]
                if (
                    role == "user"
                    and "user" not in response_messages
                    and not explicit_turn
                ):
                    turn = key = f"line-{line}"
                content = payload.get("message") or payload.get("text")
                legacy_messages.append((role, content, at, ref, key))
            elif sub in ("error", "task_failed"):
                trace.add(
                    "error",
                    "実行エラー",
                    at,
                    ref,
                    turn=key,
                    content=payload.get("message"),
                )
        elif kind == "response_item":
            if sub == "message" and payload.get("role") in ("user", "assistant"):
                role = payload["role"]
                parts = payload.get("content")
                texts = (
                    [
                        obj(part)["text"]
                        for part in parts
                        if isinstance(obj(part).get("text"), str)
                    ]
                    if isinstance(parts, list)
                    else [parts]
                )
                if (
                    role == "user"
                    and obj(record.get("metadata")).get("client_authored") is False
                ):
                    remaining = []
                    for text in texts:
                        received = (
                            codex_agent_reply(text) if isinstance(text, str) else None
                        )
                        if received:
                            activity, reply = received
                            trace.add(
                                "agent",
                                "サブエージェントからの返信",
                                at,
                                ref,
                                turn=turn,
                                agent_activity=activity,
                                agent_reply=reply,
                            )
                        else:
                            remaining.append(text)
                    if not remaining:
                        continue
                    texts = remaining
                if role == "user" and not explicit_turn:
                    turn = f"line-{line}"
                content = "\n".join(text for text in texts if isinstance(text, str))
                synthetic = (
                    role == "user"
                    and isinstance(content, str)
                    and content.rstrip().endswith("</environment_context>")
                    and content.startswith(
                        ("# AGENTS.md instructions", "<environment_context>")
                    )
                    and not obj(record.get("metadata")).get("client_authored")
                )
                trace.add(
                    role,
                    "ユーザー入力" if role == "user" else "応答",
                    at,
                    ref,
                    turn=turn,
                    content=content,
                    synthetic=synthetic,
                )
                response_messages.add(role)
            elif sub == "reasoning":
                response_messages.add("reasoning")
                trace.add(
                    "reasoning",
                    "推論イベント",
                    at,
                    ref,
                    turn=turn,
                    content=payload.get("summary"),
                )
            elif sub in (
                "function_call",
                "custom_tool_call",
                "tool_call",
                "web_search_call",
            ):
                args = payload.get(
                    "arguments", payload.get("input", payload.get("action"))
                )
                inputs = decoded(args)
                name = payload.get("name")
                agent_request = None
                activity = None
                if name in (
                    "spawn_agent",
                    "followup_task",
                    "send_message",
                    "send_input",
                ) and payload.get("namespace") in (None, "collaboration"):
                    agent_request = plain_text(obj(inputs).get("message"))
                    target = obj(inputs).get("target") or obj(inputs).get("task_name")
                    activity = {
                        "action": name,
                        "recipient": target if isinstance(target, str) else None,
                        "type": obj(inputs).get("agent_type"),
                    }
                event = trace.add(
                    "tool",
                    name or ("web_search" if sub == "web_search_call" else "ツール"),
                    at,
                    ref,
                    turn=turn,
                    call=payload.get("call_id"),
                    content=inputs,
                    agent_request=agent_request,
                    agent_activity=activity,
                )
                if agent_request and isinstance(event.get("call"), str):
                    pending_agent_sends[event["call"]] = event
            elif sub in (
                "function_call_output",
                "custom_tool_call_output",
                "tool_result",
            ):
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    pending_agent_sends.pop(call_id, None)
                output = payload.get("output")
                out = obj(decoded(output))
                exit_code = out.get("exit_code")
                failed = (
                    payload.get("is_error") is True
                    or out.get("is_error") is True
                    or (type(exit_code) is int and exit_code != 0)
                )
                trace.add(
                    "result",
                    "ツール結果",
                    at,
                    ref,
                    turn=turn,
                    call=payload.get("call_id"),
                    failed=failed,
                    exit_code=exit_code,
                    content=output,
                )
    for role, content, at, ref, key in legacy_messages:
        if role not in response_messages:
            label = {
                "user": "ユーザー入力",
                "assistant": "応答",
                "reasoning": "推論イベント",
            }[role]
            trace.add(role, label, at, ref, turn=key, content=content)
    samples = list(response_usage.values())
    seen_totals = response_totals
    for at, ref, key, usage, total in legacy_usage:
        total_key = tuple(sorted(total.items()))
        if total_key in seen_totals or (
            response_usage and (not total or not response_totals)
        ):
            continue
        if total:
            seen_totals.add(total_key)
        samples.append((at, ref, key, usage))
    for at, ref, key, usage in samples:
        trace.add(
            "model",
            "モデル使用量",
            at,
            ref,
            turn=key,
            model=model_by_turn.get(key),
            tokens=usage,
        )


def open_tokens(data):
    tokens = usage_tokens(
        data,
        {
            "input": "input",
            "output": "output",
            "reasoning": "reasoning",
            "total": "total",
        },
    )
    tokens.update(
        usage_tokens(
            obj(data).get("cache"), {"cache_read": "read", "cache_write": "write"}
        )
    )
    return tokens


def opencode_messages(trace, messages):
    for msg, at, ref, parts in messages:
        role = msg.get("role")
        turn = (
            msg.get("parentID")
            if role == "assistant"
            else msg.get("id") or ref.get("id")
        )
        if role == "assistant":
            start = timestamp(obj(msg.get("time")).get("created")) or at
            end = timestamp(obj(msg.get("time")).get("completed"))
            steps = any(part.get("type") == "step-finish" for part, _, _ in parts)
            trace.add(
                "model",
                "アシスタントメッセージ全体",
                start,
                ref,
                end=end,
                timing="message",
                turn=turn,
                model=msg.get("modelID"),
                tokens={} if steps else open_tokens(msg.get("tokens")),
                cost=None if steps else number(msg.get("cost")),
                usage_basis=not steps,
                failed=bool(msg.get("error")),
                content=msg.get("error"),
            )
        for part, part_at, part_ref in parts:
            part_type = part.get("type")
            if part_type == "text":
                kind = "user" if role == "user" else "assistant"
                trace.add(
                    kind,
                    "ユーザー入力" if kind == "user" else "応答",
                    part_at if part_at is not None or kind != "user" else at,
                    part_ref,
                    turn=turn,
                    content=part.get("text"),
                    synthetic=bool(part.get("synthetic")),
                )
            elif part_type == "reasoning":
                timing = obj(part.get("time"))
                trace.add(
                    "reasoning",
                    "推論イベント",
                    timestamp(timing.get("start")) or part_at,
                    part_ref,
                    end=timestamp(timing.get("end")) if timing.get("start") else None,
                    timing="recorded",
                    turn=turn,
                )
            elif part_type == "tool":
                state = obj(part.get("state"))
                timing = obj(state.get("time"))
                start = timestamp(timing.get("start"))
                failed = state.get("status") == "error" or bool(state.get("error"))
                trace.add(
                    "tool",
                    part.get("tool") or "ツール",
                    start or part_at,
                    part_ref,
                    end=timestamp(timing.get("end")) if start else None,
                    timing="recorded",
                    turn=turn,
                    call=part.get("callID"),
                    content=state.get("input"),
                    output=state.get("error") if failed else state.get("output"),
                    failed=failed,
                )
            elif part_type == "step-finish":
                trace.add(
                    "model",
                    "ステップ使用量",
                    part_at,
                    part_ref,
                    turn=turn,
                    model=msg.get("modelID"),
                    tokens=open_tokens(part.get("tokens")),
                    cost=number(part.get("cost")),
                    usage_basis=True,
                )


def opencode_db_messages(trace):
    sid = trace.session.id
    with closing(open_db(trace.session.path)) as connection:
        connection.execute("BEGIN")
        has_part = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='part'"
        ).fetchone()
        parts = defaultdict(list)
        if has_part:
            rows = connection.execute(
                """SELECT p.id, p.message_id, p.time_created, p.data
                   FROM part p JOIN message m ON m.id = p.message_id
                   WHERE m.session_id = ? ORDER BY p.time_created, p.id""",
                (sid,),
            )
            for row in rows:
                id_, message_id, created, data = row
                part = decoded(data)
                if not isinstance(part, dict):
                    trace.warnings["invalid_record"] += 1
                    continue
                parts[message_id].append(
                    (part, timestamp(created), {"table": "part", "id": id_})
                )
        rows = connection.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY time_created, id",
            (sid,),
        )
        for row in rows:
            id_ = row["id"]
            msg = decoded(row["data"]) if "data" in row.keys() else dict(row)
            if not isinstance(msg, dict):
                trace.warnings["invalid_record"] += 1
                continue
            msg = {"id": id_, **msg}
            if "role" in row.keys():
                msg.setdefault("role", row["role"])
            at = timestamp(row["time_created"])
            ref = {"table": "message", "id": id_}
            yield msg, at, ref, parts[id_]


def opencode_json_messages(export):
    for index, entry in enumerate(export["messages"]):
        msg = obj(obj(entry).get("info"))
        at = timestamp(obj(msg.get("time")).get("created"))
        ref = {"pointer": f"/messages/{index}/info"}
        parts = [
            (
                obj(part),
                timestamp(obj(obj(part).get("time")).get("start")),
                {"pointer": f"/messages/{index}/parts/{part_index}"},
            )
            for part_index, part in enumerate(obj(entry).get("parts") or [])
        ]
        yield msg, at, ref, parts


def opencode_jsonl(trace, records):
    trace.snapshots["turns_unavailable"] = True
    turn = None
    for line, record in records:
        ref = {"line": line}
        at = timestamp(record.get("timestamp"))
        kind = record.get("type")
        part = obj(record.get("part")) or record
        if kind == "step_start":
            continue
        if kind in ("tool_use", "step_finish"):
            part = {**part, "type": "tool" if kind == "tool_use" else "step-finish"}
            opencode_messages(
                trace, [({"parentID": turn}, None, ref, [(part, at, ref)])]
            )
        elif kind in ("text", "reasoning", "error"):
            trace.add(
                "assistant" if kind == "text" else kind,
                {"text": "応答", "reasoning": "推論イベント", "error": "実行エラー"}[
                    kind
                ],
                at,
                ref,
                content=part.get("text") or part.get("error"),
            )


def codex_record_key(record):
    kind = record.get("type")
    payload = obj(record.get("payload"))
    subtype = payload.get("type")
    if kind == "response_item":
        key = payload.get("id") or payload.get("call_id")
    elif kind == "token_usage_record":
        key = payload.get("response_id")
    elif kind == "turn_context" or (
        kind == "event_msg"
        and subtype in ("task_started", "task_complete", "turn_aborted")
    ):
        key = payload.get("turn_id")
    elif kind == "event_msg" and subtype == "item_completed":
        item = obj(payload.get("item"))
        key = item.get("id")
        subtype = (subtype, item.get("type"))
    else:
        return None
    return (kind, subtype, key) if isinstance(key, str) and key else None


def own_codex_records(session, records, inherited, warnings):
    for line, record in records:
        ordinal = record.get("ordinal")
        if (
            session.history_start_ordinal is not None
            and type(ordinal) is int
            and ordinal < session.history_start_ordinal
        ) or codex_record_key(record) in inherited:
            warnings["inherited_records_skipped"] += 1
            continue
        payload = obj(record.get("payload"))
        if record.get("type") == "token_usage_record" and payload.get(
            "thread_id"
        ) not in (None, session.id):
            continue
        yield line, record


def read_trace(session, inaccessible, *, inherited=None):
    trace = Trace(session)
    if inaccessible:
        trace.warnings["inaccessible_sources"] = inaccessible
    suffix = session.path.suffix.lower()
    if session.source == "opencode" and suffix in DB_SUFFIXES:
        opencode_messages(trace, opencode_db_messages(trace))
    elif (
        session.source == "opencode"
        and suffix == ".json"
        and (export := opencode_export(session.path)) is not None
    ):
        opencode_messages(trace, opencode_json_messages(export))
    else:
        handlers = {
            "codex": codex_trace,
            "claude": claude_trace,
            "opencode": opencode_jsonl,
        }
        with session.path.open(encoding="utf-8", errors="replace") as stream:
            records = jsonl_records(stream, trace.warnings)
            if inherited is not None and session.source == "codex":
                trace.snapshots["own_usage"] = True
                records = own_codex_records(session, records, inherited, trace.warnings)
            handlers[session.source](trace, records)
    if not trace.events:
        raise EmptyTrace(
            f"指定セッション {session.id} には表示できるイベントがありません"
        )
    return trace


def subagent_sessions(root, unavailable):
    if root.source == "claude":
        parent_path = (
            root.path.parent.parent
            if root.path.parent.name == "subagents"
            else root.path.with_suffix("")
        )
        paths = (parent_path / "subagents").glob("agent-*.jsonl")
        sessions = []
        for path in sorted(paths):
            if not path.is_symlink() and path.is_file():
                try:
                    sessions.append(jsonl_session(path, "claude"))
                except OSError:
                    unavailable.add(path.stem)
        return sessions
    sessions, _ = discover("codex", None, None, read_updated=False)
    known_paths = {session.path for session in sessions}
    for path in root.path.parent.glob("*.jsonl"):
        if path in known_paths or path.is_symlink() or not path.is_file():
            continue
        try:
            records = jsonl_header(path)
        except OSError:
            continue
        if identify_jsonl(records) == "codex":
            sessions.append(jsonl_session(path, "codex", records))
    return sessions


def codex_history_keys(path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        return {
            key
            for _, record in jsonl_records(stream)
            if (key := codex_record_key(record)) is not None
        }


def collect_subagent_traces(root, input_paths=None):
    source = root.session.source
    if input_paths is None:
        input_paths = set()
    unavailable = set()
    unavailable_history = set()
    catalog = subagent_sessions(root.session, unavailable)
    by_id = defaultdict(list)
    by_parent = defaultdict(set)
    for session in catalog:
        by_id[session.id].append(session)
        if session.parent_id:
            by_parent[session.parent_id].add(session.id)
    if source == "codex" and root.session.parent:
        inherited = set()
        ancestor = root.session.parent_id
        visited = {root.session.id}
        while ancestor and ancestor not in visited and len(by_id[ancestor]) == 1:
            visited.add(ancestor)
            session = by_id[ancestor][0]
            input_paths.add(session.path.resolve())
            try:
                inherited.update(codex_history_keys(session.path))
            except OSError:
                unavailable_history.add(session.id)
            ancestor = session.parent_id
        original_warnings = root.warnings
        root = read_trace(root.session, 0, inherited=inherited)
        for key in ("empty_sessions_skipped", "inaccessible_sources"):
            if original_warnings.get(key):
                root.warnings[key] = original_warnings[key]
    else:
        inherited = set()

    entries = [(root, None, "親セッション")]
    histories = [inherited]
    seen = {root.session.id}

    def load_child(session_id, parent_index, label):
        if session_id in seen:
            return
        candidates = by_id[session_id]
        if len(candidates) != 1:
            unavailable.add(session_id)
            return
        session = candidates[0]
        if source == "codex" and session.parent_id not in (
            None,
            entries[parent_index][0].session.id,
        ):
            return
        inherited = histories[parent_index] if source == "codex" else None
        input_paths.add(session.path.resolve())
        try:
            trace = read_trace(session, 0, inherited=inherited)
        except (OSError, EmptyTrace):
            unavailable.add(session_id)
            return
        seen.add(session_id)
        entries.append((trace, parent_index, label or session_id))
        histories.append(inherited)

    index = 0
    while True:
        while index < len(entries):
            trace = entries[index][0]
            pair_tools(trace)
            if source == "codex":
                try:
                    histories[index] = histories[index] | codex_history_keys(
                        trace.session.path
                    )
                except OSError:
                    unavailable_history.add(trace.session.id)
            for event in trace.events:
                agent = event.get("agent_activity") or {}
                identifier = agent.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                if source == "claude":
                    identifier = "agent-" + identifier
                else:
                    candidates = by_id.get(identifier, [])
                    if candidates and not any(
                        child.parent and child.parent_id in (None, trace.session.id)
                        for child in candidates
                    ):
                        continue
                    if not candidates and agent.get("status") not in (
                        "started",
                        "completed",
                    ):
                        continue
                load_child(
                    identifier,
                    index,
                    agent.get("name") or agent.get("type"),
                )
            if source == "codex":
                for identifier in sorted(by_parent[trace.session.id]):
                    load_child(identifier, index, None)
            index += 1
        if source != "claude" or root.session.parent:
            break
        remaining = sorted(set(by_id) - seen - unavailable)
        if not remaining:
            break
        load_child(remaining[0], 0, None)
    if unavailable:
        root.warnings["subagents_unavailable"] += len(unavailable)
    if unavailable_history:
        root.warnings["inherited_history_unavailable"] += len(unavailable_history)
    return entries


def session_info(session):
    return {
        "source": session.source,
        "agent": AGENTS[session.source],
        "id": session.id,
        "cwd": session.cwd,
        "path": str(session.path),
    }


def combine_subagent_traces(entries):
    combined = Trace(entries[0][0].session)
    sessions = []
    for index, (trace, parent_index, label) in enumerate(entries):
        observed = [event for event in trace.events if not event.get("combined")]
        sessions.append(
            {
                **session_info(trace.session),
                "label": label,
                "parent_index": parent_index,
                "token_summary": token_summary(trace, observed),
                "cost_summary": cost_summary(trace, observed),
            }
        )
        combined.warnings.update(
            {
                key: combined.warnings[key] + value
                for key, value in trace.warnings.items()
            }
        )
        offset = len(combined.events)
        for event in trace.events:
            merged = {**event, "id": event["id"] + offset, "session_index": index}
            if event.get("turn") is not None:
                merged["turn"] = (index, event["turn"])
            for link in ("tool_id", "result_id"):
                if event.get(link) is not None:
                    merged[link] = event[link] + offset
            combined.events.append(merged)
    return combined, sessions


def pair_tools(trace):
    pending = defaultdict(dict)
    for event in trace.events:
        call = event.get("call")
        if not call:
            continue
        turn = event.get("turn")
        if event["kind"] == "tool":
            if event.get("timing") != "recorded":
                pending[call][turn] = event
            continue
        if event["kind"] != "result":
            continue
        candidates = pending.get(call, {})
        start = candidates.pop(turn, None)
        if start is None and len(candidates) == 1:
            _, start = candidates.popitem()
        if start is None:
            trace.warnings["unmatched_result"] += 1
            continue
        start.update(
            output=event.get("content"),
            answers=event.get("answers"),
            failed=event.get("failed", False),
            exit_code=event.get("exit_code"),
            result_id=event["id"],
        )
        if event.get("agent_activity"):
            start["agent_activity"] = {
                **(start.get("agent_activity") or {}),
                **event["agent_activity"],
            }
        if event.get("agent_reply"):
            start["agent_reply"] = event["agent_reply"]
        start["refs"].extend(event["refs"])
        event["tool_id"] = start["id"]
        event["label"] = f"{start['label']} の結果"
        if start["start"] is not None and event["start"] is not None:
            if event["start"] >= start["start"]:
                start.update(end=event["start"], timing="paired")
                event["combined"] = True
            else:
                trace.warnings["invalid_interval"] += 1


def fingerprint_event(event):
    if event.get("content") is None or event.get("output") is None:
        return None
    return json.dumps(
        [event["label"], event["content"], event["output"], bool(event.get("failed"))],
        sort_keys=True,
        ensure_ascii=False,
    )


def repetition_findings(events):
    per_turn = defaultdict(list)
    for event in events:
        if event["kind"] == "tool" and event.get("turn"):
            per_turn[event["turn"]].append((fingerprint_event(event), event))
    for calls in per_turn.values():
        i = 0
        while i < len(calls):
            for period in (1, 2):
                pattern = [signature for signature, _ in calls[i : i + period]]
                if len(pattern) != period or None in pattern:
                    continue
                count = 1
                j = i + period
                while pattern == [signature for signature, _ in calls[j : j + period]]:
                    count += 1
                    j += period
                if count >= REPEAT_COUNT:
                    repeated = [event for _, event in calls[i:j]]
                    yield {
                        "kind": "repeat",
                        "label": " → ".join(
                            event["label"] for event in repeated[:period]
                        ),
                        "count": count,
                        "period": period,
                        "event_ids": [event["id"] for event in repeated],
                        "reason": "同じ引数・同じ結果の呼び出しが同一ターン内で連続",
                    }
                    i = j
                    break
            else:
                i += 1


def token_summary(trace, observed):
    totals = defaultdict(int)
    for event in observed:
        for key, value in event.get("tokens", {}).items():
            totals[key] += value
    if "tokens" in trace.snapshots and not trace.snapshots.get("own_usage"):
        tokens, ref = trace.snapshots["tokens"]
        return {"values": tokens, "basis": "Codex CLI · 最新の累積値", "ref": ref}
    bases = {
        "claude": "Claude Code · メッセージID単位",
        "codex": "Codex CLI · ログで確認できたリクエスト",
        "opencode": "OpenCode · ステップ単位（内訳がない応答はメッセージ単位）",
    }
    if trace.snapshots.get("own_usage"):
        bases["codex"] = "Codex CLI · 子セッション固有のリクエスト"
    return {"values": dict(totals), "basis": bases[trace.session.source]}


def cost_summary(trace, observed):
    if "cost" in trace.snapshots:
        cost = trace.snapshots["cost"]
        status = "partial" if cost["partial"] else "recorded"
        return {
            **cost,
            "currency": "USD",
            "status": status if cost["value"] is not None else "unknown",
        }
    opencode = [event for event in observed if event.get("usage_basis")]
    prices = [event for event in opencode if event.get("cost") is not None]
    models = defaultdict(float)
    for event in prices:
        model = event.get("model")
        if model:
            models[model] += event["cost"]
    return {
        "value": sum(event["cost"] for event in prices) if prices else None,
        "status": ("recorded" if len(prices) == len(opencode) else "partial")
        if prices
        else "unknown",
        "currency": "USD",
        "basis": "OpenCode · ステップまたはメッセージのログ記録値"
        if prices
        else "金額の記録なし",
        "models": dict(models),
    }


def event_token_count(source, event):
    tokens = event.get("tokens") or {}
    if "total" in tokens:
        return tokens["total"]
    fields = (
        ("input", "output", "cache_read", "cache_write")
        if source == "claude"
        else ("input", "output")
    )
    components = [tokens[key] for key in fields if key in tokens]
    return sum(components) if components else None


def usage_highlights(source, events):
    def rankings(label, scores):
        return [
            {"metric": label, "value": value, "event_ids": [event["id"]]}
            for value, event in sorted(
                scores, key=lambda item: (-item[0], item[1]["id"])
            )[:5]
        ]

    token_count = [
        (count, event)
        for event in events
        if (count := event_token_count(source, event)) is not None
    ]
    cost = [(event["cost"], event) for event in events if event.get("cost") is not None]
    return rankings("cost", cost) + rankings("tokens", token_count)


def observed_gaps(timed):
    per_turn = defaultdict(list)
    for event in timed:
        if event.get("turn") and event.get("timing") not in ("turn", "message"):
            per_turn[event["turn"]].append(event)
    gaps = []
    for events in per_turn.values():
        end = None
        previous_id = None
        for event in sorted(events, key=lambda entry: (entry["start"], entry["id"])):
            start = event["start"]
            if end is not None and start - end >= GAP_SECONDS:
                gaps.append(
                    {
                        "kind": "gap",
                        "label": "ログ上の空白",
                        "seconds": round(start - end, 3),
                        "reason": "同一ターン内で記録された区間・イベントの間に30秒以上の空白",
                        "event_ids": [previous_id, event["id"]],
                    }
                )
            last = event["end"] if event["end"] is not None else start
            if end is None or last > end:
                end, previous_id = last, event["id"]
    return sorted(gaps, key=lambda gap: -gap["seconds"])[:5]


def diagnostic_report(trace):
    events = [event for event in trace.events if not event.get("combined")]
    timed = [event for event in events if event["start"] is not None]
    start = min((event["start"] for event in timed), default=None)
    end = max(
        (
            event["end"] if event["end"] is not None else event["start"]
            for event in timed
        ),
        default=None,
    )

    spans = [
        event for event in timed if event["end"] is not None and event["kind"] != "turn"
    ]
    spans.sort(key=lambda e: (-(e["end"] - e["start"]), e["id"]))
    longest = [
        {
            "kind": "slow" if duration >= SLOW_SECONDS else "duration",
            "label": event["label"],
            "event_ids": [event["id"]],
            "seconds": round(duration, 3),
            "action": action_name(event),
            "reason": "ログから計測した区間が10秒以上"
            if duration >= SLOW_SECONDS
            else "ログから計測した区間",
        }
        for event in spans[:8]
        if (duration := event["end"] - event["start"]) >= 0
    ]
    repeats = list(repetition_findings(events))
    errors = [
        {
            "kind": "error",
            "label": event["label"],
            "event_ids": [event["id"]],
            "reason": "エラーまたは非ゼロ終了が記録された",
        }
        for event in events
        if (event["kind"] == "error" or event.get("failed"))
        and not event.get("tool_id")
    ]
    session = trace.session
    limitations = []
    if all(event["kind"] == "user" for event in events):
        limitations.append(
            f"ユーザー入力 {len(events)} 件のみ。応答・ツール実行・完了を判断できません。"
        )
    missing_turns = trace.snapshots.get("turns_unavailable") or any(
        event["kind"] == "tool" and not event.get("turn") for event in events
    )
    if missing_turns:
        limitations.append(
            "ターンの対応が不明な記録があります。その範囲では反復を判定できません。"
        )
    uncovered = [
        event
        for event in events
        if event["kind"] == "tool"
        and event["end"] is None
        and not event.get("result_id")
        and event.get("output") is None
    ]
    if uncovered:
        trace.warnings["unmatched_tool"] = len(uncovered)

    return {
        "session": session_info(session),
        "range": {
            "start": start,
            "end": end,
            "elapsed": round(end - start, 3) if start is not None else None,
        },
        "counts": {
            "events": len(events),
            "timed": len(timed),
            "spans": len(spans),
            "errors": len(errors),
            "repeats": None if missing_turns and not repeats else len(repeats),
        },
        "token_summary": token_summary(trace, events),
        "cost_summary": cost_summary(trace, events),
        "warnings": dict(trace.warnings),
        "limitations": limitations,
        "longest": longest,
        "usage_highlights": usage_highlights(session.source, events),
        "findings": errors
        + repeats
        + [item for item in longest if item["kind"] == "slow"]
        + observed_gaps(timed),
    }


# Only these fixed operation names enter metadata-only reports.
COMMAND_ACTIONS = {
    "rg": "コードやファイルを検索",
    "grep": "テキストを検索",
    "cat": "ファイルを読む",
    "sed": "テキストを処理",
    "ls": "ファイルを一覧表示",
    "ruff": "Pythonコードを検査・整形",
    "pytest": "テストを実行",
    "node": "JavaScriptを実行",
    "git": "Gitを操作",
    "python3": "Pythonを実行",
    "python": "Pythonを実行",
    "find": "ファイルを検索",
}
TOOL_ACTIONS = {
    "read": "ファイルを読む",
    "write": "ファイルを書き込む",
    "edit": "ファイルを編集",
    "multiedit": "ファイルを編集",
    "apply_patch": "変更をファイルに適用",
    "wait": "開始済みの処理を待つ",
    "request_user_input": "ユーザーの回答待ち",
    "askuserquestion": "ユーザーへの質問",
    "sleep": "指定時間の待機",
    "exec": "ツールを呼び出す",
    "web_search": "ウェブを検索",
}
FILE_TOOL_ACTIONS = {"read", "write", "edit", "multiedit", "apply_patch"}
QUESTION_TOOLS = {"askuserquestion", "request_user_input"}


def question_details(event):
    questions = obj(event.get("content")).get("questions")
    if not isinstance(questions, list):
        return {}
    output = event.get("output")
    answers = obj(event.get("answers") or obj(decoded(output)).get("answers"))
    items = []
    for raw in questions:
        question = obj(raw)
        prompt = question.get("question")
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        raw_options = question.get("options")
        options = (
            [
                {"label": option["label"], "description": option.get("description")}
                for option in (obj(option) for option in raw_options)
                if isinstance(option.get("label"), str)
            ]
            if isinstance(raw_options, list)
            else []
        )
        question_id = question.get("id")
        answer = answers.get(question_id if isinstance(question_id, str) else prompt)
        if isinstance(answer, dict):
            answer = answer.get("answers")
        if isinstance(answer, list):
            answer = "\n".join(value for value in answer if isinstance(value, str))
        items.append(
            {
                "question": prompt,
                "options": options,
                "answer": answer if isinstance(answer, str) else None,
                "multiple": question.get("multiSelect") is True,
            }
        )
    if not items:
        return {}
    result = {"items": items}
    if (
        not answers
        and isinstance(output, str)
        and output.strip()
        and not event.get("failed")
    ):
        result["recorded_response"] = output
    return result


def patch_targets(text):
    if not isinstance(text, str) or not text.lstrip().startswith("*** Begin Patch"):
        return []
    targets = []
    for line in text.splitlines():
        for prefix, action in (
            ("*** Add File: ", "add"),
            ("*** Update File: ", "update"),
            ("*** Delete File: ", "delete"),
        ):
            if line.startswith(prefix):
                targets.append({"path": line[len(prefix) :], "action": action})
                break
        else:
            if line.startswith("*** Move to: ") and targets:
                targets[-1]["destination"] = line.removeprefix("*** Move to: ")
    return targets


def command_action(command):
    try:
        script = command if isinstance(command, str) else ""
        words = command if isinstance(command, list) else shlex.split(command or "")
        if (
            len(words) >= 3
            and Path(words[0]).name in ("sh", "bash", "zsh")
            and "c" in words[1]
        ):
            script = words[2]
            words = shlex.split(script.splitlines()[0])
        if not words:
            return None
        if Path(words[0]).name == "env":
            words = words[1:]
            while len(words) >= 2 and words[0] in ("-u", "--unset"):
                words = words[2:]
        program = Path(words[0]).name
        if program == "gh" and words[1:3] == ["run", "watch"]:
            return "CI完了待ち（gh run watch）"
        if program == "sleep":
            return "指定時間の待機（sleep）"
        if any(separator in script.strip() for separator in ("\n", ";", "|", "&&")):
            first = program if program in COMMAND_ACTIONS else "シェル"
            return "シェルスクリプトを実行（先頭 " + first + "）"
        if words[1:3] == ["-m", "unittest"] and program in ("python", "python3"):
            return "Pythonのテストを実行"
        if program == "git" and words[1:2] == ["status"]:
            return "Gitの変更状況を確認"
        if program in COMMAND_ACTIONS:
            return COMMAND_ACTIONS[program] + "（" + program + "）"
    except (ValueError, TypeError, IndexError):
        pass
    return None


def action_name(event):
    kind = event["kind"]
    if kind == "user" and event.get("synthetic"):
        return "セッションの補足情報を受信"
    if kind == "command":
        return command_action(event.get("command")) or "シェルコマンドを実行"
    if kind == "tool":
        name = event["label"].lower().split(".")[-1]
        if name in ("agent", "task") and event.get("agent_activity") is not None:
            return "サブエージェントに委任"
        if event.get("agent_request") and event.get("agent_activity"):
            return (
                "サブエージェントへの依頼"
                if name == "spawn_agent"
                else "サブエージェントとのやり取り"
            )
        if name in ("bash", "exec_command"):
            args = obj(event.get("content"))
            return (
                command_action(args.get("cmd") or args.get("command"))
                or "シェルコマンドを実行"
            )
        return TOOL_ACTIONS.get(name, event["label"])
    return {
        "user": "ユーザーの入力を受信",
        "assistant": "エージェントがメッセージを送信",
        "turn": "依頼を受けてから処理を終えるまで"
        if event["end"] is not None
        else "依頼の処理を開始",
        "reasoning": "モデルが推論",
        "model": event["label"],
    }.get(kind, event["label"])


PUBLIC_EVENT_FIELDS = {
    "id",
    "kind",
    "label",
    "start",
    "end",
    "timing",
    "refs",
    "failed",
    "interrupted",
    "exit_code",
    "model",
    "tokens",
    "cost",
    "result_id",
    "tool_id",
    "api_status",
    "first_token_ms",
    "reason",
    "stop_reason",
    "server",
    "status",
    "session_index",
}


def operation_details(event):
    kind = event["kind"]
    if kind not in (
        "command",
        "tool",
        "file_change",
        "error",
        "result",
    ) and not event.get("failed"):
        return {}
    raw = event.get("content")
    args = obj(raw)
    tool = event["label"].lower().split(".")[-1] if kind == "tool" else None
    paths = [
        args[key]
        for key in ("file_path", "filePath", "filepath", "path", "filename")
        if isinstance(args.get(key), str) and args[key]
    ]
    changes = []
    if kind == "file_change":
        for path, entry in args.items():
            change = obj(entry)
            item = {"path": path, "action": change.get("type")}
            if isinstance(change.get("move_path"), str) and change["move_path"]:
                item["destination"] = change["move_path"]
            changes.append(item)
    elif tool == "apply_patch":
        patch = (
            raw if isinstance(raw, str) else args.get("patch") or args.get("patchText")
        )
        changes = patch_targets(patch)
    paths.extend(
        part["path"]
        for part in (event.get("parsed_cmd") or [])
        if isinstance(obj(part).get("path"), str)
    )
    command = event.get("command") or args.get("cmd") or args.get("command")
    if isinstance(command, list) and all(isinstance(word, str) for word in command):
        command = shlex.join(command)
    if (
        not command
        and str(event["label"]).lower().split(".")[-1] == "exec"
        and isinstance(event.get("content"), str)
    ):
        command = event["content"]
    details = {
        "paths": list(dict.fromkeys(paths)),
        "changes": changes,
        "file_operation": (
            kind == "file_change"
            or tool in FILE_TOOL_ACTIONS
            or any(
                obj(part).get("type") == "read"
                for part in event.get("parsed_cmd") or []
            )
        ),
        "command": command,
        "cwd": event.get("cwd") or args.get("workdir") or args.get("cwd"),
    }
    if tool == "read":
        read_range = {
            key: value
            for key in ("offset", "limit")
            if (value := number(args.get(key))) is not None
        }
        if read_range:
            details["read_range"] = read_range
    if event.get("failed") or kind == "error":
        output = event.get("output")
        if output is None and kind != "tool":
            output = event.get("content")
        if output is not None:
            if event.get("server") and isinstance(obj(output).get("content"), list):
                texts = [
                    block["text"]
                    for block in (obj(block) for block in output["content"])
                    if block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ]
                if texts:
                    output = "\n".join(texts)
            message = (
                output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=False, indent=2)
            )
            details["error_output"] = (
                message
                if len(message) <= 6000
                else "…（末尾6000文字）\n" + message[-6000:]
            )
    return {key: value for key, value in details.items() if value}


def claude_user_message(content):
    # Claude Code wraps slash commands in these fields, in either order.
    command = re.fullmatch(
        r"\s*(?:<command-message>[^<>]*</command-message>\s*)?"
        r"<command-name>(?P<name>/[^<>\s]+)</command-name>\s*"
        r"(?:<command-message>[^<>]*</command-message>\s*)?"
        r"(?:<command-args>(?P<args>.*)</command-args>\s*)?",
        content,
        flags=re.DOTALL,
    )
    if not command:
        return content
    name = command["name"]
    arguments = command["args"]
    return f"{name}\n{arguments}" if arguments and arguments.strip() else name


def public_events(trace, include_content, *, operations=False, messages=False):
    fields = (
        PUBLIC_EVENT_FIELDS | {"content", "command", "output"}
        if include_content
        else PUBLIC_EVENT_FIELDS
    )
    events = []
    for event in trace.events:
        if event.get("combined"):
            continue
        public = {
            key: value
            for key, value in event.items()
            if key in fields and value is not None
        }
        public["action"] = action_name(event)
        if (count := event_token_count(trace.session.source, event)) is not None:
            public["token_count"] = count
        if operations and (details := operation_details(event)):
            public["operation"] = details
        if operations and event.get("agent_activity"):
            public["agent_activity"] = event["agent_activity"]
        if messages or include_content:
            for field in ("agent_request", "agent_reply"):
                if event.get(field):
                    public[field] = event[field]
        content = event.get("content")
        if (
            (messages or include_content)
            and event["kind"] in ("user", "assistant")
            and not event.get("synthetic")
            and isinstance(content, str)
            and content.strip()
        ):
            public["message"] = (
                claude_user_message(content)
                if trace.session.source == "claude" and event["kind"] == "user"
                else content
            )
            public.pop("content", None)
        if (
            (messages or include_content)
            and event["kind"] == "tool"
            and event["label"].lower().split(".")[-1] in QUESTION_TOOLS
            and (questionnaire := question_details(event))
        ):
            public["questionnaire"] = questionnaire
        events.append(public)
    return events


def diagnostic_events(events, report):
    ids = {
        event_id
        for finding in report["findings"]
        + report["longest"]
        + report["usage_highlights"]
        for event_id in finding["event_ids"]
    }
    return [event for event in events if event["id"] in ids]


def json_text(value, *, indent=None):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent)


def write_private(path, content):
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def render_html(report, events, include_content):
    assets = Path(__file__).resolve().parents[1] / "assets"
    template = (assets / "viewer.html").read_text(encoding="utf-8")
    stylesheet = (assets / "document.css").read_text(encoding="utf-8")
    license_text = (assets / "document.css.LICENSE").read_text(encoding="utf-8")
    template = template.replace(
        "__DOCUMENT_CSS__", f"/*\n{license_text}*/\n{stylesheet}"
    )
    payload = json_text(
        {
            **report,
            "events": events,
            "include_content": include_content,
        }
    ).replace("<", r"\u003c")
    return template.replace(
        "__TRACE_TITLE__", html.escape(report["session"]["agent"])
    ).replace("__TRACE_DATA__", payload)


def main():
    parser = argparse.ArgumentParser(
        description="ローカルのエージェントログから単一HTMLの実行タイムラインを生成"
    )
    parser.add_argument("--source", choices=["auto", *AGENTS], default="auto")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--project", metavar="PATH", help="作業パスが一致する最新のセッション"
    )
    scope.add_argument("--session", metavar="ID", help="完全一致するセッションID")
    parser.add_argument(
        "--file", metavar="PATH", help="JSONL、OpenCodeエクスポート、またはSQLite"
    )
    parser.add_argument(
        "--output",
        default="trace.html",
        metavar="PATH",
        help="HTMLの出力先（既定値 trace.html）",
    )
    parser.add_argument("--diagnostics", metavar="PATH", help="追加分析用のJSON出力先")
    subagents = parser.add_mutually_exclusive_group()
    subagents.add_argument(
        "--include-subagents",
        action="store_true",
        default=None,
        help="Claude CodeとCodexの親・子セッションを同じ時間軸に表示（既定）",
    )
    subagents.add_argument(
        "--no-include-subagents",
        dest="include_subagents",
        action="store_false",
        help="選択したセッションだけを表示",
    )
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--metadata-only",
        action="store_true",
        help="HTMLから会話本文、コマンド、対象パス、失敗出力を省く",
    )
    visibility.add_argument(
        "--include-content",
        action="store_true",
        help="HTMLにメッセージ本文とツール入出力を含める",
    )
    parser.add_argument(
        "--open", action="store_true", help="生成後に既定のブラウザで開く"
    )
    args = parser.parse_args()
    if args.project and args.file:
        parser.error("--project と --file は同時に指定できません")

    try:
        trace = select_trace(args)
        session = trace.session
        input_paths = {session.path.resolve()}
        sessions = []
        supports_subagents = session.source in ("claude", "codex")
        if args.include_subagents and not supports_subagents:
            raise ValueError("子セッションの統合はClaude CodeとCodexに対応しています")
        if supports_subagents and args.include_subagents is not False:
            try:
                entries = collect_subagent_traces(trace, input_paths)
            except EmptyTrace:
                # A selected Codex child may contain only inherited history.
                if args.include_subagents:
                    raise
                pair_tools(trace)
            else:
                if len(entries) > 1 or args.include_subagents:
                    trace, sessions = combine_subagent_traces(entries)
                else:
                    trace = entries[0][0]
        else:
            pair_tools(trace)
        output = Path(args.output).expanduser().resolve()
        diagnostics = (
            Path(args.diagnostics).expanduser().resolve() if args.diagnostics else None
        )
        paths = [p for p in (output, diagnostics) if p is not None]
        sqlite_paths = (
            {
                session.path.with_name(session.path.name + suffix)
                for suffix in ("-wal", "-shm", "-journal")
            }
            if session.path.suffix.lower() in DB_SUFFIXES
            else set()
        )
        if len(set(paths)) != len(paths) or (input_paths | sqlite_paths).intersection(
            paths
        ):
            raise ValueError(
                "出力先は入力ログと分け、HTMLと診断JSONには別のパスを指定してください"
            )
        for path in paths:
            if not path.parent.is_dir() or path.is_dir():
                raise ValueError(
                    f"出力先に既存ディレクトリ内のファイルパスを指定してください: {path}"
                )
        report = diagnostic_report(trace)
        if sessions:
            report.update(
                sessions=sessions,
                token_summary=sessions[0]["token_summary"],
                cost_summary=sessions[0]["cost_summary"],
            )
        rendered = render_html(
            report,
            public_events(
                trace,
                args.include_content,
                operations=not args.metadata_only,
                messages=not args.metadata_only,
            ),
            args.include_content,
        )
        if diagnostics is not None:
            evidence = diagnostic_events(public_events(trace, False), report)
            write_private(
                diagnostics,
                json_text({**report, "evidence": evidence}, indent=2) + "\n",
            )
        write_private(output, rendered)
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        parser.exit(1, f"[agent-trace] {exc}\n")

    print(
        f"[agent-trace] {AGENTS[session.source]} {session.id} · {report['counts']['events']} イベント · {len(report['findings'])} 件の診断候補"
    )
    for limitation in report["limitations"]:
        print(f"[agent-trace] {limitation}")
    if skipped := trace.warnings.get("empty_sessions_skipped"):
        print(f"[agent-trace] 表示対象のないセッション {skipped} 件をスキップ")
    if sessions:
        print(f"[agent-trace] 子セッション {len(sessions) - 1} 件を統合")
    if unavailable := trace.warnings.get("subagents_unavailable"):
        print(
            f"[agent-trace] 読み込めなかった、または識別できなかった子セッション {unavailable} 件"
        )
    print(f"[agent-trace] HTML {output}")
    if diagnostics is not None:
        print(f"[agent-trace] JSON {diagnostics}")
    if args.open and not webbrowser.open(output.as_uri()):
        print(
            "[agent-trace] ブラウザを開けませんでした。出力したHTMLを開いてください。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
