import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_p1_workflow import create_workspace, running_app


def test_knowledge_import_search_source_and_delete(tmp_path: Path) -> None:
    source = tmp_path / "manual.md"
    source.write_text("步骤: 配置本地知识库\n警告: 不得泄露隐私数据", encoding="utf-8")
    with running_app(tmp_path) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url)
        create_workspace(page, "知识库 E2E 项目")
        page.get_by_label("描述你的论文或报告需求").fill("知识库 E2E 项目")
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile("已保存"))).to_be_visible()
        page.get_by_role("button", name="知识库", exact=True).click()
        page.get_by_label("导入知识文件").set_input_files(source)
        page.get_by_role("button", name="导入并建立索引").click()
        expect(page.get_by_text(re.compile("已导入"))).to_be_visible()
        page.get_by_label("检索问题").fill("隐私数据")
        page.get_by_role("button", name="检索知识").click()
        expect(page.locator(".knowledge-card")).to_have_count(1)
        expect(page.get_by_text("无外部来源")).to_be_visible()
        expect(page.get_by_text(re.compile("引用资格"))).to_be_visible()
        page.on("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="删除条目").first.click()
        expect(page.get_by_text("知识条目已删除")).to_be_visible()
        browser.close()
