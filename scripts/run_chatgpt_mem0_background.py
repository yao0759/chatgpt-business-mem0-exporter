"""Run the authorized historical import without storing credentials on disk."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
work = root / "state"
out = root / "scripts"
export = Path(os.environ.get("CHATGPT_EXPORT_DIR", str(root / "export"))).expanduser().resolve()
work.mkdir(parents=True, exist_ok=True)
status_path = work / "chatgpt_mem0_background_status.json"
log_path = work / "chatgpt_mem0_background.log"


def status(phase, **extra):
    value = {"phase": phase, "at": datetime.now(timezone.utc).isoformat(), **extra}
    status_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def execute(phase, args):
    status(phase)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting {phase}\n")
        log.flush()
        result = subprocess.run([sys.executable, *args], stdout=log, stderr=subprocess.STDOUT,
                                env=os.environ.copy(), check=False)
        log.write(f"[{datetime.now(timezone.utc).isoformat()}] {phase} exit={result.returncode}\n")
        log.flush()
    if result.returncode:
        status("failed", failed_phase=phase, exit_code=result.returncode)
        raise SystemExit(result.returncode)


def main():
    if not os.environ.get("MEM0_API_KEY") or not os.environ.get("ARK_API_KEY"):
        status("failed", reason="Missing MEM0_API_KEY or ARK_API_KEY")
        raise SystemExit(2)
    execute("raw_import", [str(out / "import_chatgpt_business_to_mem0.py"),
                           str(export / "markdown"), "--checkpoint",
                           str(work / "chatgpt_mem0_checkpoint.jsonl"), "--execute"])
    prep = [str(out / "distill_chatgpt_business_to_mem0.py"), "prepare",
            str(export / "json"), "--output", str(work / "chatgpt_distilled_review.jsonl"),
            "--max-chars", "5000"]
    execute("distill_prepare", prep)
    # A second pass retries conversations that had transient model/API failures.
    execute("distill_retry", prep)
    execute("distilled_import", [str(out / "distill_chatgpt_business_to_mem0.py"), "import",
                                 str(work / "chatgpt_distilled_review.jsonl"),
                                 "--checkpoint", str(work / "chatgpt_distilled_import_checkpoint.jsonl"),
                                 "--execute"])
    status("completed")


if __name__ == "__main__":
    main()
