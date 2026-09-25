"""Run the new workspace import after both prerequisite background jobs finish."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "state"
OUT = ROOT / "scripts"
SOURCE = Path(os.environ.get("CHATGPT_CURRENT_EXPORT_DIR", str(ROOT / "export"))).expanduser().resolve()
WORK.mkdir(parents=True, exist_ok=True)
STATUS = WORK / "chatgpt_current_mem0_status.json"
LOG = WORK / "chatgpt_current_mem0.log"
REVIEW = WORK / "chatgpt_current_distilled_review.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(phase: str, **details) -> None:
    STATUS.write_text(json.dumps({"phase": phase, "at": now(), **details},
                                 ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prerequisites() -> tuple[bool, str, int]:
    old = read_json(WORK / "chatgpt_mem0_background_status.json")
    if old.get("phase") != "completed":
        return False, f"旧工作区导入状态：{old.get('phase')}", 0
    export = read_json(SOURCE / "export_status.json")
    if export.get("phase") != "completed":
        return False, f"新工作区导出状态：{export.get('phase')}", 0
    manifest = read_json(SOURCE / "conversation_manifest.json")
    if not manifest.get("scan_complete"):
        return False, "链接清单扫描未完成", 0
    ids = {item["conversation_id"] for item in manifest["conversations"]}
    saved = {p.stem for p in (SOURCE / "json").glob("*.json")}
    markdown = {p.stem for p in (SOURCE / "markdown").glob("*.md")}
    if ids != saved or ids != markdown:
        return False, f"文件不齐：链接 {len(ids)}，JSON {len(saved)}，Markdown {len(markdown)}", len(ids)
    return True, "ready", len(ids)


def run(phase: str, *command: str) -> None:
    write_status(phase)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[{now()}] {phase}\n")
        stream.flush()
        result = subprocess.run([sys.executable, *command], stdout=stream,
                                stderr=subprocess.STDOUT, env=os.environ.copy(), check=False)
        stream.write(f"[{now()}] exit={result.returncode}\n")
    if result.returncode:
        write_status("failed", failed_phase=phase, exit_code=result.returncode)
        raise SystemExit(result.returncode)


def main() -> int:
    if STATUS.exists() and read_json(STATUS).get("phase") == "completed":
        print("新工作区已导入完成")
        return 0
    try:
        ready, reason, total = prerequisites()
    except (FileNotFoundError, ValueError, KeyError) as exc:
        ready, reason, total = False, f"前置状态缺失：{exc}", 0
    if not ready:
        write_status("waiting", reason=reason)
        print(reason)
        return 3
    if not os.environ.get("MEM0_API_KEY") or not os.environ.get("ARK_API_KEY"):
        write_status("waiting_credentials", reason="运行环境缺少 MEM0_API_KEY 或 ARK_API_KEY")
        return 4
    run("raw_import", str(OUT / "import_chatgpt_business_json_to_mem0.py"),
        str(SOURCE / "json"), "--checkpoint", str(WORK / "chatgpt_current_raw_checkpoint.jsonl"),
        "--execute")
    prepare = (str(OUT / "distill_chatgpt_business_to_mem0.py"), "prepare",
               str(SOURCE / "json"), "--output", str(REVIEW), "--max-chars", "5000")
    run("distill_prepare", *prepare)
    run("distill_retry", *prepare)
    done_ids = set()
    for line in REVIEW.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("record_type") == "conversation_done":
            done_ids.add(record["conversation_id"])
    if len(done_ids) != total:
        write_status("failed", failed_phase="distill_retry", reason="仍有对话未成功提炼",
                     prepared=len(done_ids), total=total)
        return 5
    run("distilled_import", str(OUT / "distill_chatgpt_business_to_mem0.py"), "import",
        str(REVIEW), "--checkpoint", str(WORK / "chatgpt_current_distilled_checkpoint.jsonl"),
        "--agent-id", "chatgpt-business-current-distilled", "--execute")
    write_status("completed", conversations=total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
