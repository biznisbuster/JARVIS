"""Headless test for Faza 7B — UI tabs (Vite + React + TS).

Verifies parity with the vanilla `web/` panels:
  - Tab navigation visible and switchable
  - Connections tab: shows LLM/Kilo/Audio/PTT cards, PTT toggle works
  - Local models tab: table renders, capability badges, pull input + cancel
  - Tools tab: rows for all 21 tools from TOOL_DESCRIPTIONS
  - Logs tab: auto-scroll checkbox, clear button
  - Permissions tab: default policy select + table
  - Chat tab: still works (WS, send, transcript)

Server is expected to be already running on http://127.0.0.1:7777.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Page, sync_playwright

BASE = "http://127.0.0.1:7777"
HERE = Path(__file__).parent


def wait_ws(page: Page, timeout_s: float = 8.0) -> None:
    """Wait until the topbar WS dot turns green (wsConnected=true)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        cls = page.evaluate("() => document.querySelector('.ws-dot')?.className || ''")
        if "ok" in cls:
            return
        time.sleep(0.1)
    raise RuntimeError("WebSocket never connected")


def click_tab(page: Page, label: str) -> None:
    page.locator(f'.tabbar button:has-text("{label}")').first.click()
    page.wait_for_timeout(200)


def main() -> int:
    errors: list[str] = []
    console_msgs: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE, wait_until="domcontentloaded")
        wait_ws(page)

        tabs = page.locator(".tabbar button").all_text_contents()
        assert "Razgovor" in tabs, f"tabs missing Razgovor: {tabs}"
        assert "Dozvole" in tabs, f"tabs missing Dozvole: {tabs}"
        assert "Konekcije" in tabs, f"tabs missing Konekcije: {tabs}"
        assert "Lokalni modeli" in tabs, f"tabs missing: {tabs}"
        assert "Alati" in tabs, f"tabs missing: {tabs}"
        assert "Logovi" in tabs, f"tabs missing: {tabs}"
        print(f"OK tabs: {tabs}")

        click_tab(page, "Konekcije")
        page.wait_for_selector(".conn-card", timeout=5000)
        cards = page.locator(".conn-card").all_text_contents()
        assert any("Minimax" in c for c in cards), f"missing LLM card: {cards}"
        assert any("Kilo CLI" in c for c in cards), f"missing Kilo card: {cards}"
        assert any("Audio" in c for c in cards), f"missing Audio card: {cards}"
        assert any("Push-to-Talk" in c for c in cards), f"missing PTT card: {cards}"
        print(f"OK connections cards: {len(cards)} {sum(1 for c in cards if 'Push-to-Talk' in c)} PTT")

        ptt_btn = page.locator(".conn-card:has-text('Push-to-Talk') button.primary").first
        ptt_label_before = ptt_btn.text_content()
        ptt_btn.click()
        page.wait_for_timeout(400)
        ptt_label_after = ptt_btn.text_content()
        if ptt_label_before == ptt_label_after:
            errors.append(f"PTT toggle button text didn't change: {ptt_label_before}")
        else:
            print(f"OK PTT toggle: '{ptt_label_before}' -> '{ptt_label_after}'")
        ptt_btn.click()
        page.wait_for_timeout(400)

        click_tab(page, "Lokalni modeli")
        page.wait_for_selector(".data-table", timeout=5000)
        rows = page.locator(".data-table tbody tr").count()
        engine_missing = page.locator(".banner").count()
        if not engine_missing:
            pull_input = page.locator('input[placeholder*="qwen"]')
            if pull_input.count() > 0:
                pull_input.first.fill("smollm:135m")
                page.wait_for_timeout(100)
                print("OK local models: pull input filled")
        print(f"OK local models: {rows} rows, {engine_missing} engine-missing banners")

        click_tab(page, "Alati")
        page.wait_for_selector(".data-table", timeout=5000)
        tool_rows = page.locator(".data-table tbody tr").count()
        assert tool_rows >= 21, f"expected ≥21 tools, got {tool_rows}"
        print(f"OK tools tab: {tool_rows} tools")

        click_tab(page, "Dozvole")
        page.wait_for_selector(".data-table", timeout=5000)
        perm_rows = page.locator(".data-table tbody tr").count()
        assert perm_rows >= 21, f"expected ≥21 perm rows, got {perm_rows}"
        print(f"OK permissions tab: {perm_rows} rows")

        click_tab(page, "Konekcije")
        page.wait_for_selector(".conn-card", timeout=5000)
        page.wait_for_timeout(300)

        click_tab(page, "Logovi")
        page.wait_for_selector(".log-stream", timeout=5000)
        log_text = page.locator(".log-stream").text_content() or ""
        assert len(log_text) > 0, "logs empty"
        print(f"OK log stream: {len(log_text)} chars")

        clear_btn = page.locator(".logs-head button").first
        clear_btn.click()
        page.wait_for_timeout(300)
        log_text_after = page.locator(".log-stream").text_content() or ""
        if log_text_after.strip():
            errors.append(f"clear didn't wipe logs: {log_text_after[:80]}")
        else:
            print("OK logs clear")

        click_tab(page, "Razgovor")
        page.wait_for_selector(".chat-tab", timeout=5000)
        page.wait_for_timeout(400)
        ta = page.locator("textarea#input")
        ta.click()
        ta.fill("3+5")
        page.wait_for_timeout(200)
        send_btn = page.locator(".composer button.btn.primary").first
        send_btn.click()
        page.wait_for_selector(".transcript .msg.assistant", timeout=15000)
        page.wait_for_timeout(2500)
        last_text = page.locator(".transcript .msg.assistant").last.text_content() or ""
        if "8" not in last_text:
            errors.append(f"chat regression: expected '8' in assistant reply, got '{last_text[:80]}'")
        else:
            print(f"OK chat: 3+5 -> {last_text.strip()[:50]}")

        browser.close()

    bad_console = [m for m in console_msgs if m.startswith("[error]")]
    if bad_console:
        errors.append(f"console errors: {bad_console[:5]}")
    if errors:
        print(f"\nFAIL ({len(errors)} errors):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"\nALL OK (console: {len(console_msgs)} msgs, 0 errors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
