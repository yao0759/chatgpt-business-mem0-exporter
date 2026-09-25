#!/usr/bin/env python3
"""Export selected ChatGPT Business conversations from the signed-in Edge session.

The archive is independent of earlier workspaces. JSON is the source of truth;
Markdown is generated only after the page passes completeness checks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

from export_chatgpt_business_edge import (
    _replace_code_placeholders,
    atomic_write_text,
    connect_edge_over_cdp,
    conversation_id_from_url,
    discover_cdp_endpoint,
    extract_messages,
    utc_now,
    write_markdown,
)

DEFAULT_URL = "https://chatgpt.com/c/6ab37c55-f6f4-83e8-8a28-97ac222300cd"
ERROR_MARKERS = (
    "too many requests", "rate limit", "you have been blocked",
    "something went wrong", "unable to load conversation",
    "访问过于频繁", "请求过于频繁", "无法加载对话",
)


def conversation_hash(messages: list[dict]) -> str:
    body = [{"role": m.get("role"), "text": m.get("text"),
             "html": m.get("html"), "code_blocks": m.get("code_blocks")}
            for m in messages]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid_messages(messages: list[dict]) -> bool:
    return bool(messages) and all(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "system", "tool")
        and (str(m.get("text") or "").strip() or str(m.get("html") or "").strip())
        for m in messages
    )


def write_complete_markdown(path: Path, record: dict) -> None:
    rendered = dict(record)
    rendered["messages"] = []
    for i, message in enumerate(record["messages"], 1):
        item = dict(message)
        blocks = item.get("code_blocks") or []
        if any(not str(block.get("text") or "").strip() for block in blocks):
            raise RuntimeError(f"第 {i} 条消息含空代码块；拒绝保存不完整 Markdown")
        item["html"] = _replace_code_placeholders(
            str(item.get("html") or ""), blocks, f"message-{i}"
        )
        rendered["messages"].append(item)
    write_markdown(path, rendered)
    markdown = path.read_text(encoding="utf-8")
    headings = len(re.findall(r"^## \d+\. (?:User|Assistant|System|Tool)$", markdown, re.M))
    expected_blocks = sum(len(m.get("code_blocks") or []) for m in record["messages"])
    fences = len(re.findall(r"^`{3,}", markdown, re.M))
    if headings != len(record["messages"]) or fences < 2 * expected_blocks:
        raise RuntimeError(
            f"Markdown 校验失败: 消息 {headings}/{len(record['messages'])}, "
            f"代码围栏 {fences}/{2 * expected_blocks}"
        )


async def wait_until_stable(page, timeout_seconds: int = 75) -> list[dict]:
    """Require three identical nonempty observations, then verify extraction."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last = None
    stable = 0
    while asyncio.get_running_loop().time() < deadline:
        if not page.url.startswith("https://chatgpt.com/c/"):
            raise RuntimeError(f"页面跳转到非对话地址: {page.url}")
        body = (await page.locator("body").inner_text(timeout=10000)).lower()
        if any(marker in body for marker in ERROR_MARKERS):
            raise RuntimeError("页面显示访问限制或加载错误")
        nodes = page.locator("[data-message-author-role]")
        count = await nodes.count()
        if count:
            signature = await nodes.evaluate_all(
                "els => els.map(e => [e.getAttribute('data-message-author-role'), (e.innerText||'').length, (e.innerText||'').slice(-100)])"
            )
            if signature == last:
                stable += 1
            else:
                last, stable = signature, 1
            if stable >= 3:
                messages = await extract_messages(page)
                if len(messages) == count and valid_messages(messages):
                    return messages
                last, stable = None, 0
        await page.wait_for_timeout(2200)
    raise RuntimeError("对话未在规定时间内完整稳定加载；未保存导出文件")


def read_urls(args) -> list[str]:
    urls = list(args.url)
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not manifest.get("scan_complete"):
            raise RuntimeError("链接清单尚未完整扫描，请先重新运行 --scan-links-only")
        urls += [entry["url"] for entry in manifest.get("conversations", [])]
    if args.url_file:
        urls += [line.strip() for line in args.url_file.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    return list(dict.fromkeys(urls))


def write_export_status(path: Path, phase: str, **details) -> None:
    atomic_write_text(path, json.dumps({"phase": phase, "at": utc_now(), **details},
                                       ensure_ascii=False, indent=2))


async def scan_links(page, manifest_path: Path, max_rounds: int) -> dict:
    """Scan the sidebar without visiting any conversation body."""
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    found: dict[str, str] = {}
    stable_at_bottom = 0
    previous_height = None
    complete = False
    for round_no in range(1, max_rounds + 1):
        state = await page.evaluate("""() => {
          const links = [...document.querySelectorAll('a[href*="/c/"]')];
          const rows = links.map(a => ({href:a.href, title:(a.innerText||a.textContent||'').trim()}));
          let box = null, best = 0;
          for (const a of links) {
            for (let p=a.parentElement; p; p=p.parentElement) {
              const s=getComputedStyle(p), range=p.scrollHeight-p.clientHeight;
              if ((s.overflowY==='auto'||s.overflowY==='scroll') && range>best) {
                box=p; best=range;
              }
            }
          }
        if (!box) return {rows, atBottom:true, scrollable:false, scrollHeight:0};
          const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-12;
          if (!atBottom) {
            box.scrollTop=Math.min(box.scrollTop+Math.max(250,box.clientHeight*0.65),box.scrollHeight);
            box.dispatchEvent(new Event('scroll',{bubbles:true}));
          }
          return {rows, atBottom, scrollable:true, scrollHeight:box.scrollHeight};
        }""")
        before = len(found)
        for row in state["rows"]:
            url = row["href"].split("?", 1)[0].rstrip("/")
            if re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9-]+", url):
                found[url] = re.sub(r"\s+", " ", row["title"]).strip() or conversation_id_from_url(url)
        at_bottom = bool(state["atBottom"])
        height = state["scrollHeight"]
        stable_at_bottom = (stable_at_bottom + 1 if at_bottom and len(found) == before
                            and height == previous_height else 0)
        previous_height = height
        manifest = {
            "scanned_at": utc_now(), "scan_complete": False,
            "conversation_count": len(found), "scan_round": round_no,
            "coverage": "sidebar_links_only",
            "conversations": [{"conversation_id": conversation_id_from_url(url),
                               "title": title, "url": url} for url, title in found.items()],
        }
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        if round_no == 1 or round_no % 10 == 0 or stable_at_bottom >= 10:
            print(f"扫描轮次 {round_no}: 已发现 {len(found)} 个链接；到底={at_bottom}", flush=True)
        if found and stable_at_bottom >= 10:
            complete = True
            break
        await page.wait_for_timeout(1800 + random.randint(0, 500))
    manifest["scan_complete"] = complete
    manifest["scanned_at"] = utc_now()
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


async def export(args) -> int:
    edge_dir = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data"
    urls = read_urls(args)
    async with async_playwright() as p:
        browser = await connect_edge_over_cdp(p.chromium, discover_cdp_endpoint(edge_dir))
        context = next((c for c in browser.contexts if any("chatgpt.com" in pg.url for pg in c.pages)), None)
        if context is None:
            raise RuntimeError("已登录的 Edge 中没有 ChatGPT 页面")
        page = await context.new_page()
        try:
            if args.scan_links_only:
                args.output.mkdir(parents=True, exist_ok=True)
                manifest_path = args.output / "conversation_manifest.json"
                manifest = await scan_links(page, manifest_path, args.max_scan_rounds)
                print(f"链接清单: {manifest_path}；共 {manifest['conversation_count']} 条；完整={manifest['scan_complete']}", flush=True)
                return 0 if manifest["scan_complete"] else 1
            if not urls:
                raise RuntimeError("请用 --url、--url-file 或 --scan-visible 指定对话")
            json_dir = args.output / "json"
            md_dir = args.output / "markdown"
            json_dir.mkdir(parents=True, exist_ok=True)
            md_dir.mkdir(parents=True, exist_ok=True)
            status_path = args.output / "export_status.json"
            failures = 0
            processed = 0
            write_export_status(status_path, "running", total=len(urls), processed=0, failures=0)
            for i, url in enumerate(urls, 1):
                if not re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9-]+/?", url):
                    print(f"[{i}/{len(urls)}] 无效对话链接，跳过: {url}", flush=True)
                    failures += 1
                    processed += 1
                    write_export_status(status_path, "running", total=len(urls), processed=processed, failures=failures)
                    continue
                cid = conversation_id_from_url(url)
                target_json = json_dir / f"{cid}.json"
                target_md = md_dir / f"{cid}.md"
                print(f"[{i}/{len(urls)}] {url}", flush=True)
                for attempt in range(1, args.retries + 1):
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        if response and response.status in (403, 429):
                            raise RuntimeError(f"HTTP {response.status} 访问限制")
                        messages = await wait_until_stable(page, args.load_timeout)
                        title = (await page.title()).removesuffix(" - ChatGPT").strip() or cid
                        record = {"conversation_id": cid, "title": title, "url": url,
                                  "exported_at": utc_now(), "messages": messages,
                                  "generated_files": [], "content_sha256": conversation_hash(messages),
                                  "export_quality": "stable_dom_v1",
                                  "markdown_renderer": "code_blocks_v1"}
                        if target_json.exists():
                            old = json.loads(target_json.read_text(encoding="utf-8"))
                            if (old.get("content_sha256") == record["content_sha256"]
                                    and old.get("markdown_renderer") == "code_blocks_v1"
                                    and target_md.exists()):
                                print(f"  无变化，消息 {len(messages)} 条", flush=True)
                                break
                        staged_md = target_md.with_name(target_md.name + f".tmp.{uuid.uuid4().hex}")
                        try:
                            write_complete_markdown(staged_md, record)
                            os.replace(staged_md, target_md)
                        finally:
                            staged_md.unlink(missing_ok=True)
                        atomic_write_text(target_json, json.dumps(record, ensure_ascii=False, indent=2))
                        print(f"  已保存 {len(messages)} 条消息: {target_md}", flush=True)
                        break
                    except Exception as exc:
                        print(f"  第 {attempt} 次失败: {exc}", flush=True)
                        if attempt == args.retries:
                            failures += 1
                            with (args.output / "errors.jsonl").open("a", encoding="utf-8") as fp:
                                fp.write(json.dumps({"at": utc_now(), "url": url, "error": str(exc)}, ensure_ascii=False) + "\n")
                            if "访问限制" in str(exc) or "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                                write_export_status(status_path, "paused_rate_limit", total=len(urls),
                                                    processed=processed, failures=failures, last_url=url)
                                return 2
                        else:
                            await asyncio.sleep(min(120, args.cooldown * 2 ** (attempt - 1)) + random.uniform(0, 3))
                processed += 1
                write_export_status(status_path, "running", total=len(urls), processed=processed, failures=failures)
                if i < len(urls):
                    await asyncio.sleep(args.cooldown + random.uniform(0, 3))
            write_export_status(status_path, "completed" if not failures else "completed_with_errors",
                                total=len(urls), processed=processed, failures=failures)
            return 1 if failures else 0
        finally:
            await page.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", default=[], help="可重复传入多个对话链接")
    parser.add_argument("--url-file", type=Path, help="每行一个对话链接")
    parser.add_argument("--manifest", type=Path, help="读取完整扫描得到的 conversation_manifest.json")
    parser.add_argument("--scan-links-only", action="store_true", help="只滚动扫描全部对话链接，不导出正文")
    parser.add_argument("--max-scan-rounds", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path("export"))
    parser.add_argument("--load-timeout", type=int, default=75)
    parser.add_argument("--cooldown", type=int, default=15)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.retries <= 5 or args.cooldown < 5:
        parser.error("--retries 为 1–5，--cooldown 至少 5 秒")
    try:
        return asyncio.run(export(args))
    except Exception as exc:
        print(f"导出停止: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
