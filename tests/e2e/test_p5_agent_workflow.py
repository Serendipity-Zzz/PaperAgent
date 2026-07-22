import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.e2e.test_p1_workflow import create_workspace, running_app


def test_requirement_layers_field_confirmation_outline_and_plan_survive_panel(
    tmp_path: Path,
) -> None:
    with running_app(tmp_path) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(url)
        request = "写一篇 5000 字的本地智能体实验报告, 输出 docx pdf, 引用文献"
        create_workspace(page, "需求理解 E2E")
        page.get_by_label("描述你的论文或报告需求").fill(request)
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile("已保存"))).to_be_visible()
        page.get_by_role("button", name="Agent 计划").click()
        expect(page.get_by_role("heading", name="需求四层表示")).to_be_visible()
        expect(page.get_by_text(re.compile("用户原文"))).to_be_visible()
        expect(page.locator(".agent-card").first.get_by_text(request, exact=True)).to_be_visible()
        expect(page.get_by_text("2. 规范化需求")).to_be_visible()
        expect(page.get_by_text("3. 科学化候选")).to_be_visible()
        expect(page.get_by_text("4. 已确认执行版")).to_be_visible()
        expect(page.get_by_text(re.compile("explicit_user · 95%")).first).to_be_visible()
        while page.get_by_role("button", name="接受候选").count():
            page.get_by_role("button", name="接受候选").first.click()
        page.get_by_role("button", name="整体确认 Requirement Spec").click()
        expect(page.get_by_text(re.compile("Requirement Spec 已确认"))).to_be_visible()
        expect(page.get_by_text(re.compile("confirmed · v1"))).to_be_visible()
        expect(page.get_by_role("heading", name="已冻结框架")).to_be_visible()
        expect(page.get_by_text(re.compile("环境与方法"))).to_be_visible()
        expect(page.get_by_text("执行前需审批").first).to_be_visible()
        page.get_by_role("button", name="关闭文件预览").click()
        expect(page.get_by_role("main")).to_have_attribute("data-preview-open", "false")
        browser.close()
