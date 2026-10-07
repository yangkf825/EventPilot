"""Actual Chromium checks of the rendered-page observation contract."""
import asyncio

import pytest

from eventarena.browser import Browser


@pytest.mark.browser
def test_rendered_observation_excludes_hidden_dom_and_source_text(tmp_path):
    async def check():
        async with Browser(tmp_path, headless=True) as browser:
            await browser.page.set_content('''<!doctype html><html><head>
                <style>.collapsed { display:none; } .invisible { visibility:hidden; }</style>
                </head><body><main>Visible research evidence</main>
                <div class="collapsed">HIDDEN_MENU_VARIANT</div>
                <div class="invisible">INVISIBLE_CONTENT</div>
                <script>window.SOURCE_SCRIPT_MARKER = true;</script>
                <style>/* SOURCE_STYLE_MARKER */</style>
                <button>Observed control</button>
                <button class="collapsed">Hidden control</button>
                </body></html>''')
            detached = await browser.page.evaluate('''() => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('script,style,noscript').forEach(el=>el.remove());
                return clone.innerText;
            }''')
            assert "HIDDEN_MENU_VARIANT" in detached
            result = await browser.observe()
            assert "Visible research evidence" in result["text"]
            for hidden in ("HIDDEN_MENU_VARIANT", "INVISIBLE_CONTENT",
                           "SOURCE_SCRIPT_MARKER", "SOURCE_STYLE_MARKER", "Hidden control"):
                assert hidden not in result["text"]
            assert [c["text"] for c in result["candidates"]] == ["Observed control"]
    asyncio.run(check())


@pytest.mark.browser
@pytest.mark.parametrize("original_style", [None, "display: flex !important; color: red;"])
def test_overlay_excluded_and_exact_inline_style_restored(tmp_path, original_style):
    async def check():
        async with Browser(tmp_path, headless=True) as browser:
            await browser.page.set_content('''<body><p>Ordinary page evidence</p>
                <div data-eventarena-panel><button>EVENT_PANEL_SECRET</button></div></body>''')
            if original_style is not None:
                await browser.page.locator("[data-eventarena-panel]").evaluate(
                    "(el, value) => el.setAttribute('style', value)", original_style)
            for _ in range(2):
                result = await browser.observe()
                assert "Ordinary page evidence" in result["text"]
                assert "EVENT_PANEL_SECRET" not in result["text"]
                assert not result["candidates"]
                assert await browser.page.locator("[data-eventarena-panel]").get_attribute("style") == original_style
            assert "EVENT_PANEL_SECRET" in await browser.page.evaluate("document.body.innerText")
    asyncio.run(check())


@pytest.mark.browser
def test_full_rendered_document_retains_long_and_below_fold_evidence(tmp_path):
    async def check():
        async with Browser(tmp_path, headless=True) as browser:
            evidence = "complete evidence " * 30000
            await browser.page.set_content("<body><main><p>" + evidence +
                                          "</p><p>FINAL_BELOW_FOLD_EVIDENCE</p></main></body>")
            result = await browser.observe()
            assert len(result["text"]) > 450000
            assert result["text"].count("complete evidence") == 30000
            assert result["text"].endswith("FINAL_BELOW_FOLD_EVIDENCE")
    asyncio.run(check())
