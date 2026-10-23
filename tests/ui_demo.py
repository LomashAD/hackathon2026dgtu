#!/usr/bin/env python
"""Сквозной прогон демо-сценария (раздел 8 PLAN.md) через реальный веб-интерфейс в Chrome (Playwright).

Требует: запущенное приложение (./run.sh) и `pip install playwright` (в отдельном окружении, это dev-инструмент).
Сохраняет скриншоты в docs/screenshots/ и проверяет, что браузер не делает запросов к внешним хостам.

  python tests/ui_demo.py [--url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
DRAFT = ROOT / "samples" / "Черновик_с_ошибками.docx"

FILL_ANSWERS = [
    "подготовке проекта Регламента электронного архива",
    "подготовки и согласования проекта Регламента электронного архива",
    "начальник Управления архивного хранения Департамента ДОУ И. И. Иванов",
    "главный специалист Департамента ДОУ П. П. Петрова",
    "ведущий эксперт Департамента ИТ С. С. Сидоров; юрисконсульт Юридического департамента А. А. Смирнова",
    "15.12.2026",
    "разработать проект Регламента электронного архива",
    "проект Регламента в Департамент документационного обеспечения",
    "утверждения Регламента электронного архива",
    "директора Департамента документационного обеспечения И. И. Кузнецова",
    "А. Б. Васильев",
    "нет",
]


class Chat:
    def __init__(self, page):
        self.page = page

    async def replies(self) -> int:
        return await self.page.locator("text=⏱").count()

    async def wait_reply(self, before: int, timeout: float = 90) -> str:
        for _ in range(int(timeout * 4)):
            if await self.replies() > before:
                await self.page.wait_for_timeout(400)
                return await self.page.inner_text("body")
            await self.page.wait_for_timeout(250)
        raise TimeoutError("нет ответа бота")

    async def say(self, text: str) -> str:
        before = await self.replies()
        box = self.page.locator("#chat-input")
        await box.fill(text)
        await box.press("Enter")
        return await self.wait_reply(before)

    async def click(self, label: str) -> str:
        before = await self.replies()
        await self.page.get_by_role("button", name=label).last.click()
        return await self.wait_reply(before)

    async def shot(self, name: str) -> None:
        await self.page.screenshot(path=str(SHOTS / name), full_page=False)


def check(cond: bool, msg: str, failures: list[str]) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


async def main(url: str) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    external: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 1000}, locale="ru-RU", accept_downloads=True)
        page = await ctx.new_page()
        local = re.compile(r"^(https?|wss?)://(localhost|127\.0\.0\.1)|^(data|blob):")
        page.on("request", lambda r: external.append(r.url) if not local.match(r.url) else None)
        await page.goto(url.rstrip("/") + "/?chat", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        chat = Chat(page)

        print("1–3. Вопрос своими словами → положения с % релевантности → ответ простым языком со ссылками")
        body = await chat.say("Мне поручили собрать рабочую группу для подготовки регламента. Каким документом это оформляется?")
        check("релевантность" in body and "п. 9.7.1" in body, "найден п. 9.7.1 с процентом релевантности", failures)
        check("Источники" in body, "есть блок источников", failures)
        await chat.shot("01_question_sources.png")
        marker = "Полные тексты источников ответа"
        n0 = await page.get_by_text(marker).count()
        await page.get_by_role("button", name="📄 Тексты пунктов").last.click()
        await page.get_by_text(marker).nth(n0).wait_for(timeout=15000)
        await page.wait_for_timeout(500)
        body = await page.inner_text("body")
        check("Распоряжение издается по вопросам" in body, "по кнопке показан полный текст пунктов-источников", failures)
        await chat.shot("01b_source_texts.png")

        print("4. Уточнение недостающей информации")
        body = await chat.say("С кем его нужно согласовать?")
        check("Уточните" in body, "бот задал уточняющий вопрос", failures)
        await chat.shot("02_clarification.png")
        body = await chat.click("Распоряжение")
        check("Департамент ДОУ" in body and "Кадровое подразделение" in body, "согласующие из Матрицы (строка 6)", failures)
        check("3 рабочих дня" in body, "срок рассмотрения из п. 10.3", failures)
        await chat.shot("03_approval_matrix.png")

        print("5–6. Подбор шаблона, заполнение вопросами, выдача .docx")
        body = await chat.say("Подготовь черновик распоряжения о создании рабочей группы")
        check("Распоряжение о создании рабочей группы" in body and "Вопрос 1 из" in body, "подобран шаблон, первый вопрос", failures)
        await chat.shot("04_template_start.png")
        for i, ans in enumerate(FILL_ANSWERS):
            if i == 5:
                body = await chat.say("15 декабря")   # ошибка валидации
                check("распознать дату" in body, "валидация даты сработала", failures)
                await chat.shot("05_validation.png")
            body = await chat.say(ans)
        check("Черновик" in body and ".docx" in body, "черновик сформирован", failures)
        check("Чек-лист" in body, "показан чек-лист со ссылками на пункты", failures)
        await chat.shot("06_docx_ready.png")
        link = page.locator("a", has_text=re.compile(r"\.docx")).last
        try:
            async with page.expect_download(timeout=15000) as dl:
                await link.click()
            d = await dl.value
            target = SHOTS.parent / "demo_output.docx"
            await d.save_as(str(target))
            check(target.stat().st_size > 10000, f"файл скачан ({target.name}, {target.stat().st_size} байт)", failures)
        except Exception as e:  # noqa: BLE001
            check(False, f"скачивание .docx: {e}", failures)

        print("7. Вопрос без ответа в базе")
        body = await chat.say("Какая зарплата у главного специалиста?")
        check("недостаточно информации" in body, "честный отказ + куда обратиться", failures)
        await chat.shot("07_no_answer.png")

        print("8. Загрузка черновика и отчёт с замечаниями")
        before = await chat.replies()
        await page.locator("input[type=file]").first.set_input_files(str(DRAFT))
        await page.wait_for_timeout(1500)
        await page.locator("#chat-input").fill("Проверь черновик")
        await page.locator("#chat-input").press("Enter")
        body = await chat.wait_reply(before)
        check("замечаний" in body and "ОБЯЗЫВАЮ" in body, "отчёт проверки с замечаниями и пунктами", failures)
        await chat.shot("08_review.png")

        print("Виджет для портала")
        wpage = await ctx.new_page()
        wpage.on("request", lambda r: external.append(r.url) if not local.match(r.url) else None)
        await wpage.goto(url.rstrip("/") + "/widget", wait_until="networkidle")
        await wpage.click("#fab")
        await wpage.wait_for_timeout(4000)
        await wpage.screenshot(path=str(SHOTS / "09_portal_widget.png"))
        check(await wpage.locator("#panel.open").count() == 1, "поп-ап виджет открывается", failures)
        await wpage.click("#close")
        for tab, hash_ in [("Новости", "#news"), ("Документы", "#docs"), ("Сервисы", "#services"),
                           ("Раздел Департамента ДОУ", "#dou")]:
            await wpage.click(f"nav >> text={tab}")
            await wpage.wait_for_timeout(1000)
            visible = await wpage.locator(f"#tab-{hash_[1:]}:not([hidden])").count()
            check(wpage.url.endswith(hash_) and visible == 1, f"вкладка портала «{tab}» открывается", failures)
            if hash_ == "#docs":
                check(await wpage.locator("#docs-body a").count() >= 3 and await wpage.locator("#tpl-body tr").count() == 7,
                      "в «Документах» список документов базы знаний и 7 модулей", failures)
            if hash_ == "#services":
                check("работает" in await wpage.inner_text("#health"), "в «Сервисах» статус системы", failures)
        await browser.close()

    check(not external, f"нет запросов к внешним хостам (найдено: {external[:3]})", failures)
    print("\nИТОГ:", "все шаги пройдены" if not failures else f"ошибок: {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    sys.exit(asyncio.run(main(ap.parse_args().url)))
