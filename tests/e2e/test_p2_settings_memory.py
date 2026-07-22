from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_p1_workflow import running_app


def test_provider_privacy_and_memory_use_release_provider_ui(tmp_path: Path) -> None:
    with running_app(tmp_path) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url)
        page.get_by_role("button", name="设置").click()
        text_form = page.locator(
            "section", has=page.get_by_role("heading", name="新增文本 Provider")
        )
        text_form.get_by_label("Provider 类型").select_option("ollama")
        text_form.get_by_label("配置名称").fill("local-p2")
        text_form.get_by_label("API URL").fill("http://127.0.0.1:11434/v1")
        text_form.get_by_label("模型名").fill("user-selected-local-model")
        text_form.get_by_role("button", name="保存文本 Provider").click()
        expect(page.get_by_text("Provider local-p2 已真实写入本地后端")).to_be_visible()
        page.get_by_label("隐私模式").select_option("privacy-controlled")
        page.get_by_role("button", name="保存隐私设置").click()
        expect(page.get_by_text("隐私模式已保存")).to_be_visible()
        page.get_by_label("要记住的偏好").fill("优先使用简洁中文")
        page.get_by_role("button", name="写入记忆").click()
        expect(page.get_by_text("长期记忆已写入")).to_be_visible()
        page.on("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="清空长期记忆").click()
        expect(page.get_by_text("长期记忆已清空")).to_be_visible()
        browser.close()
