#!/usr/bin/env python3
"""Import an existing ChatGPT Business Markdown export into a mem0 server.

Each source file is split at message headings and, if necessary, into smaller
pieces. The original Markdown is stored verbatim with source metadata. A local
checkpoint plus a server-side metadata check make reruns safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MESSAGE_HEADING = re.compile(r"(?=^## \d+\. (?:User|Assistant|System|Tool)\s*$)", re.M)


def request_json(base: str, key: str, path: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        base.rstrip("/") + path,
        data=body,
        headers={"X-API-Key": key, "Accept": "application/json", **({"Content-Type": "application/json"} if body else {})},
        method="POST" if body else "GET",
    )
    for attempt in range(5):
        try:
            with urlopen(req, timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}") from exc
        except (URLError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(min(2 ** attempt, 12))


def split_markdown(markdown: str, max_chars: int):
    sections = MESSAGE_HEADING.split(markdown)
    if not sections:
        return []
    parts = []
    current = ""
    for section in sections:
        if not section:
            continue
        if current and len(current) + len(section) > max_chars:
            parts.append(current)
            current = ""
        while len(section) > max_chars:
            cut = section.rfind("\n", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            parts.append(section[:cut].rstrip("\n"))
            section = section[cut:].lstrip("\n")
        current += section
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def source_id(path: Path):
    return path.name.split("__", 1)[0]


def build_items(directory: Path, max_chars: int):
    for path in sorted(directory.glob("*.md")):
        markdown = path.read_text(encoding="utf-8-sig")
        chunks = split_markdown(markdown, max_chars)
        for index, chunk in enumerate(chunks, 1):
            fingerprint = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            key = f"{source_id(path)}:{index}:{fingerprint[:16]}"
            yield key, path, index, len(chunks), chunk, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Directory containing exported Markdown files")
    parser.add_argument("--base-url", default=os.environ.get("MEM0_BASE_URL", "http://127.0.0.1:18765"))
    parser.add_argument("--user-id", default=os.environ.get("MEM0_USER_ID", "default"))
    parser.add_argument("--agent-id", default="chatgpt-business-import")
    parser.add_argument("--max-chars", type=int, default=7500)
    parser.add_argument("--checkpoint", type=Path, default=Path("chatgpt_mem0_checkpoint.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="Import at most this many new chunks")
    parser.add_argument("--execute", action="store_true", help="Actually write to mem0")
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error(f"Source directory missing: {args.source}")
    if args.max_chars < 1000:
        parser.error("--max-chars must be at least 1000")
    items = list(build_items(args.source, args.max_chars))
    print(f"Source: {len(list(args.source.glob('*.md')))} conversations, {len(items)} chunks")
    if not args.execute:
        print("Dry run. Add --execute to write to mem0.")
        return 0
    api_key = os.environ.get("MEM0_API_KEY")
    if not api_key:
        parser.error("Set MEM0_API_KEY in the environment")
    completed = set()
    if args.checkpoint.exists():
        for line in args.checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if entry.get("status") == "ok":
                    completed.add(entry["source_key"])
            except (ValueError, KeyError):
                pass
    # The self-hosted compatibility API may ignore its limit parameter. Read
    # all returned metadata once so a missing checkpoint does not duplicate data.
    query = urlencode({"user_id": args.user_id, "agent_id": args.agent_id, "limit": 100000})
    existing = request_json(args.base_url, api_key, "/memories?" + query)
    records = existing.get("results", existing) if isinstance(existing, dict) else existing
    for record in records:
        metadata = record.get("metadata") or {}
        if metadata.get("source") == "chatgpt_business_export" and metadata.get("source_key"):
            completed.add(metadata["source_key"])
    print(f"Already imported: {len(completed)} chunks")
    written = 0
    failed = 0
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with args.checkpoint.open("a", encoding="utf-8") as checkpoint:
        for key, path, index, total, chunk, fingerprint in items:
            if key in completed:
                continue
            if args.limit and written >= args.limit:
                break
            metadata = {
                "source": "chatgpt_business_export",
                "source_key": key,
                "conversation_id": source_id(path),
                "title": path.stem.split("__", 1)[-1],
                "url": f"https://chatgpt.com/c/{source_id(path)}",
                "chunk_index": index,
                "chunk_count": total,
                "sha256": fingerprint,
                "format": "markdown",
            }
            try:
                result = request_json(args.base_url, api_key, "/memories", {
                    "messages": chunk,
                    "user_id": args.user_id,
                    "agent_id": args.agent_id,
                    "infer": False,
                    "metadata": metadata,
                })
                entry = {"status": "ok", "source_key": key, "response": result}
                written += 1
            except Exception as exc:
                entry = {"status": "error", "source_key": key, "error": str(exc)}
                failed += 1
                print(f"ERROR {key}: {exc}", file=sys.stderr, flush=True)
            checkpoint.write(json.dumps(entry, ensure_ascii=False) + "\n")
            checkpoint.flush()
            if written and written % 25 == 0:
                print(f"Imported {written} new chunks; errors {failed}", flush=True)
    print(f"Done: imported {written}, errors {failed}, remaining {max(0, len(items)-len(completed)-written)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
