#!/usr/bin/env python3
"""Import ChatGPT JSON messages to mem0 with explicit author roles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from distill_chatgpt_business_to_mem0 import post_json, read_jsonl, append_jsonl, redact_secrets
from export_chatgpt_business_edge import _replace_code_placeholders, html_to_markdown
from import_chatgpt_business_to_mem0 import request_json


def parts(text: str, max_chars: int):
    while text:
        if len(text) <= max_chars:
            yield text
            return
        cut = text.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def items(source: Path, max_chars: int):
    for path in sorted(source.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        cid = str(record["conversation_id"])
        title = str(record.get("title") or cid)
        url = str(record.get("url") or f"https://chatgpt.com/c/{cid}")
        for index, message in enumerate(record.get("messages") or [], 1):
            role = str(message.get("role") or "unknown")
            html = _replace_code_placeholders(str(message.get("html") or ""),
                                              message.get("code_blocks"), f"message-{index}")
            content = redact_secrets(html_to_markdown(html, str(message.get("text") or ""))).strip()
            if not content:
                continue
            chunks = list(parts(content, max_chars))
            for part_index, chunk in enumerate(chunks, 1):
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                source_key = f"{cid}:{index}:{part_index}:{digest}"
                metadata = {
                    "source": "chatgpt_business_current_raw",
                    "source_key": source_key,
                    "conversation_id": cid,
                    "conversation_title": title,
                    "url": url,
                    "role": role,
                    "message_index": index,
                    "part_index": part_index,
                    "part_count": len(chunks),
                    "sha256": digest,
                }
                label = {"user": "用户提问", "assistant": "ChatGPT 回答",
                         "system": "系统消息", "tool": "工具消息"}.get(role, role)
                body = f"对话：{title}\n来源：{url}\n第 {index} 条消息，{label}"
                if len(chunks) > 1:
                    body += f"，片段 {part_index}/{len(chunks)}"
                yield source_key, body + "\n\n" + chunk, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--base-url", default=os.environ.get("MEM0_BASE_URL", "http://127.0.0.1:18765"))
    parser.add_argument("--user-id", default=os.environ.get("MEM0_USER_ID", "default"))
    parser.add_argument("--agent-id", default="chatgpt-business-current-raw")
    parser.add_argument("--max-chars", type=int, default=7500)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error("JSON 源目录不存在")
    prepared = list(items(args.source, args.max_chars))
    print(f"待处理 {len(prepared)} 条角色标注消息片段", flush=True)
    if not args.execute:
        return 0
    api_key = os.environ.get("MEM0_API_KEY")
    if not api_key:
        parser.error("缺少 MEM0_API_KEY 环境变量")
    completed = {r["source_key"] for r in read_jsonl(args.checkpoint)
                 if r.get("status") == "ok" and r.get("source_key")}
    query = urlencode({"user_id": args.user_id, "agent_id": args.agent_id, "limit": 100000})
    existing = request_json(args.base_url, api_key, "/memories?" + query)
    records = existing.get("results", existing) if isinstance(existing, dict) else existing
    for memory in records:
        metadata = memory.get("metadata") or {}
        if metadata.get("source") == "chatgpt_business_current_raw" and metadata.get("source_key"):
            completed.add(metadata["source_key"])
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    for source_key, body, metadata in prepared:
        if source_key in completed:
            continue
        post_json(args.base_url.rstrip("/") + "/memories", {"X-API-Key": api_key}, {
            "messages": body, "user_id": args.user_id, "agent_id": args.agent_id,
            "infer": False, "metadata": metadata,
        }, retry_uncertain=False)
        append_jsonl(args.checkpoint, {"status": "ok", "source_key": source_key})
        written += 1
        if written % 25 == 0:
            print(f"已写入 {written} 条", flush=True)
    print(f"完成，新增 {written} 条", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
