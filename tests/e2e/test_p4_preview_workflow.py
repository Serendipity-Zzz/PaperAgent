import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_p1_workflow import create_workspace, running_app


def test_preview_smooth_panel_anchor_actions_and_draft_restore(tmp_path: Path) -> None:
    source = tmp_path / "evidence.py"
    source.write_text("def measured_result():\n    return 0.95\n", encoding="utf-8")
    with running_app(tmp_path) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(url)
        create_workspace(page, "预览 E2E 项目")
        page.get_by_label("描述你的论文或报告需求").fill("预览 E2E 项目")
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile("已保存"))).to_be_visible()
        draft = "关闭预览后这个草稿必须保留"
        page.get_by_label("描述你的论文或报告需求").fill(draft)

        page.get_by_role("button", name="知识库", exact=True).click()
        page.get_by_label("导入知识文件").set_input_files(source)
        page.get_by_role("button", name="导入并建立索引").click()
        expect(page.get_by_text(re.compile("已导入"))).to_be_visible()
        page.get_by_role("button", name="evidence.py", exact=False).evaluate(
            "element => element.click()"
        )
        preview = page.get_by_role("complementary", name="文件预览")
        expect(preview).to_be_visible()
        expect(page.get_by_text("code-structured", exact=False)).to_be_visible()
        expect(page.get_by_text("def measured_result():", exact=True)).to_be_visible()

        resizer = page.get_by_role("separator", name="调整右侧预览宽度")
        expect(resizer).to_be_visible()
        before_width = preview.bounding_box()
        assert before_width is not None
        resizer.press("ArrowLeft")
        page.wait_for_timeout(150)
        after_width = preview.bounding_box()
        assert after_width is not None and after_width["width"] > before_width["width"]

        page.get_by_role("button", name="最大化预览面板").click()
        expect(page.get_by_role("main")).to_have_attribute("data-preview-maximized", "true")
        page.wait_for_timeout(350)
        maximized_width = preview.bounding_box()
        assert maximized_width is not None and maximized_width["width"] > 1500
        page.get_by_role("button", name="恢复预览面板").click()
        expect(page.get_by_role("main")).to_have_attribute("data-preview-maximized", "false")
        page.get_by_label("在预览中搜索").fill("return 0.95")
        expect(page.get_by_text("return 0.95", exact=True)).to_be_visible()
        page.get_by_role("button", name="加入证据").click()
        expect(page.get_by_text(re.compile("可追溯的evidence上下文"))).to_be_visible()

        page.on("dialog", lambda dialog: dialog.accept("核对该实验值"))
        page.get_by_role("button", name="批注", exact=True).click()
        expect(page.get_by_text(re.compile("批注已保存"))).to_be_visible()
        page.get_by_role("button", name="关闭文件预览").click()
        expect(page.get_by_label("描述你的论文或报告需求")).to_have_value(draft)
        expect(page.get_by_role("main")).to_have_attribute("data-preview-open", "false")
        browser.close()
