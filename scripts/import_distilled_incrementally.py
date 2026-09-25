"""Import reviewed memories incrementally while the long extraction runs."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
work = root / "state"
work.mkdir(parents=True, exist_ok=True)
status_path = work / "chatgpt_mem0_background_status.json"
review_path = work / "chatgpt_distilled_review.jsonl"
checkpoint_path = work / "chatgpt_distilled_import_checkpoint.jsonl"
log_path = work / "chatgpt_distilled_incremental.log"
importer = root / "scripts" / "distill_chatgpt_business_to_mem0.py"


def phase():
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))["phase"]
    except (FileNotFoundError, ValueError, KeyError):
        return "unknown"


def main():
    if not os.environ.get("MEM0_API_KEY"):
        raise SystemExit("MEM0_API_KEY missing")
    while True:
        current = phase()
        if current not in {"distill_prepare", "distill_retry"}:
            return
        if review_path.exists():
            with log_path.open("a", encoding="utf-8") as log:
                result = subprocess.run([
                    sys.executable, str(importer), "import", str(review_path),
                    "--checkpoint", str(checkpoint_path), "--execute"
                ], stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy(), check=False)
                log.write(f"incremental import exit={result.returncode}\n")
                log.flush()
        time.sleep(60)


if __name__ == "__main__":
    main()
