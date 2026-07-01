from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


async def save_page_diagnostics(
    page,
    debug_dir: str | Path,
    prefix: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, str]:
    """保存页面诊断材料，方便排查不同环境下 DOM 或加载状态差异。"""
    target_dir = Path(debug_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"{prefix}_{stamp}"
    screenshot_file = target_dir / f"{base}.png"
    html_file = target_dir / f"{base}.html"
    json_file = target_dir / f"{base}.json"

    title = ""
    html = ""
    try:
        title = await page.title()
    except Exception:
        pass
    try:
        html = await page.content()
        html_file.write_text(html, encoding="utf-8")
    except Exception as exc:
        html_file.write_text(f"保存 HTML 失败：{exc}", encoding="utf-8")
    try:
        await page.screenshot(path=str(screenshot_file), full_page=True)
    except Exception:
        pass

    payload = {
        "message": message,
        "url": page.url,
        "title": title,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "details": details or {},
        "screenshot_file": str(screenshot_file),
        "html_file": str(html_file),
    }
    json_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "screenshot_file": str(screenshot_file),
        "html_file": str(html_file),
        "diagnostic_file": str(json_file),
    }
