#!/usr/bin/env python
"""Мобильная шапка портала в Chrome (Playwright): меню «бургер» и выезжающая панель справа.

Нужен запущенный помощник (./run.sh) и две учётные записи — сотрудник и администратор:
  DEMO_PASSWORD=<пароль сотрудника> ADMIN_PASSWORD=<пароль администратора> \\
  python tests/ui_mobile_header.py [--login sotrudnik] [--admin-login admin] [--url http://localhost:8000] [--shots]
--shots — заодно снять docs/screenshots/mobile_*.png.

Проверяется: на телефоне видны только название и кнопка меню (без наложения); меню открывается и закрывается
крестиком, затемнением, Esc, выбором пункта и при переходе на десктопную ширину; фокус внутри панели и возврат
на кнопку; прокрутка страницы заблокирована; кнопка помощника под затемнением; нет горизонтальной прокрутки;
«Админ-панель» — только администратору; на десктопе кнопки меню нет; ни одного запроса к внешним хостам.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SHOTS = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
PHONES = [(360, 740), (375, 812), (390, 844), (414, 896), (740, 360), (844, 390)]   # портрет и ландшафт
LOCAL = re.compile(r"^(https?|wss?)://(localhost|127\.0\.0\.1)|^(data|blob):")


def check(ok: bool, what: str, failures: list) -> None:
    print(("  OK  " if ok else "  FAIL") + "  " + what)
    if not ok:
        failures.append(what)


async def login(ctx, url: str, user: str, password: str):
    page = await ctx.new_page()
    await page.goto(url + "/login?next=%2F", wait_until="networkidle")
    await page.fill("#login", user)
    await page.fill("#password", password)
    await page.click("button[type=submit]")
    await page.wait_for_url(re.compile(r".*/(#.*)?$"), timeout=15000)
    await page.wait_for_load_state("networkidle")
    return page


async def open_tab(page, url: str, tab: str, theme: str | None = None):
    if theme:
        await page.evaluate(f"localStorage.setItem('vite-ui-theme', '{theme}')")
    await page.goto(url + "/#" + tab, wait_until="networkidle")
    await page.reload(wait_until="networkidle")   # тема читается при загрузке страницы
    await page.wait_for_timeout(400)


async def state(page) -> dict:
    return await page.evaluate("""() => {
      const r = e => { const b = document.querySelector(e)?.getBoundingClientRect(); return b && {x: b.x, y: b.y, w: b.width, h: b.height}; };
      const m = document.getElementById('site-menu'), btn = document.getElementById('menu-btn');
      return {compact: document.documentElement.classList.contains('compact-nav'),
              open: document.documentElement.classList.contains('menu-open'),
              expanded: btn.getAttribute('aria-expanded'), label: btn.getAttribute('aria-label'),
              menuVisible: getComputedStyle(m).visibility === 'visible' && getComputedStyle(m).opacity === '1',
              btn: r('#menu-btn'), brand: r('header b span'), menu: r('#site-menu'), header: r('header'),
              brandCut: (s => s.scrollWidth > s.clientWidth)(document.querySelector('header b span')),
              hscroll: document.documentElement.scrollWidth > innerWidth,
              overflow: getComputedStyle(document.documentElement).overflow,
              focus: document.activeElement?.id || document.activeElement?.textContent.trim().slice(0, 30),
              items: [...m.querySelectorAll('nav a')].filter(a => !a.hidden).map(a => a.textContent.trim()),
              cut: [...m.querySelectorAll('nav a, #me-name span, #theme, #logout')].filter(e => e.offsetParent && e.scrollWidth > e.clientWidth + 1).map(e => e.textContent.trim()),
              vw: innerWidth, vh: innerHeight};
    }""")


async def wait_closed(page):
    await page.wait_for_function("!document.documentElement.classList.contains('menu-open')", timeout=3000)
    await page.wait_for_timeout(300)   # анимация закрытия


async def main(url: str, login_name: str, password: str, admin: str, admin_password: str, shots: bool) -> int:
    failures, external = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, locale="ru-RU")
        ctx.on("request", lambda r: external.append(r.url) if not LOCAL.match(r.url) else None)
        page = await login(ctx, url, login_name, password)

        print("Телефон: шапка закрыта")
        for w, h in PHONES + [(768, 1024)]:
            await page.set_viewport_size({"width": w, "height": h})
            await open_tab(page, url, "dou")
            s = await state(page)
            check(s["compact"] and s["btn"]["w"] >= 44 and s["btn"]["h"] >= 44, f"{w}×{h}: кнопка меню 44×44", failures)
            gap = s["vw"] - (s["btn"]["x"] + s["btn"]["w"])
            check(12 <= gap <= 16, f"{w}×{h}: отступ кнопки от правого края {gap:.0f} px", failures)
            check(s["brand"]["x"] + s["brand"]["w"] <= s["btn"]["x"], f"{w}×{h}: название не наезжает на кнопку", failures)
            check(not s["hscroll"], f"{w}×{h}: нет горизонтальной прокрутки", failures)
            check(await page.locator("header nav").is_hidden() and await page.locator("#logout").is_hidden(),
                  f"{w}×{h}: вкладки и кнопки шапки скрыты в меню", failures)
        await page.set_viewport_size({"width": 390, "height": 844})
        await open_tab(page, url, "dou", "dark")
        if shots:
            await page.screenshot(path=str(SHOTS / "mobile_header_closed.png"))

        print("Телефон: меню открывается и закрывается")
        await page.click("#menu-btn")
        await page.wait_for_timeout(350)
        s = await state(page)
        check(s["open"] and s["menuVisible"] and s["expanded"] == "true" and s["label"] == "Закрыть меню",
              "кнопка открывает меню (aria-expanded, «Закрыть меню»)", failures)
        check(abs(s["menu"]["x"] + s["menu"]["w"] - s["vw"]) < 1 and 0.8 * s["vw"] - 1 <= s["menu"]["w"] <= 360
              and abs(s["menu"]["h"] - s["vh"]) < 1, f"панель справа на всю высоту, ширина {s['menu']['w']:.0f} px", failures)
        check(s["overflow"] == "hidden", "прокрутка страницы под меню заблокирована", failures)
        check(s["items"] == ["Новости", "Документы", "Сервисы", "Раздел Департамента ДОУ"], "сотруднику — 4 вкладки без «Админ-панели»",
              failures)
        check(not s["cut"], f"текст пунктов не обрезан {s['cut']}", failures)
        check(s["focus"] == "me-name", "фокус в панели — на профиле", failures)
        fab = await page.locator("#fab").bounding_box()
        top = await page.evaluate(f"document.elementFromPoint({fab['x'] + fab['width'] / 2}, {fab['y'] + fab['height'] / 2})?.id")
        check(top != "fab", "кнопка помощника под затемнением, не поверх меню", failures)
        seen = []
        for _ in range(9):   # ловушка фокуса: Tab ходит по кругу внутри панели
            await page.keyboard.press("Tab")
            seen.append(await page.evaluate("document.activeElement.closest('#site-menu, #menu-btn') ? 1 : 0"))
        check(all(seen), "Tab не выводит фокус из панели", failures)
        if shots:
            await open_tab(page, url, "dou", "light")
            await page.click("#menu-btn")
            await page.wait_for_timeout(350)
            await page.mouse.move(5, 300)
            await page.screenshot(path=str(SHOTS / "mobile_menu_open.png"))
            await page.keyboard.press("Escape")
            await wait_closed(page)
            await open_tab(page, url, "dou", "dark")
            await page.click("#menu-btn")
            await page.wait_for_timeout(350)
            await page.mouse.move(5, 300)
            await page.screenshot(path=str(SHOTS / "mobile_menu_open_dark.png"))
        await page.keyboard.press("Escape")
        await wait_closed(page)
        s = await state(page)
        check(not s["open"] and not s["menuVisible"] and s["expanded"] == "false" and s["focus"] == "menu-btn"
              and s["overflow"] != "hidden", "Esc закрывает, фокус на кнопке, прокрутка вернулась", failures)

        await page.click("#menu-btn")
        await page.wait_for_timeout(350)
        await page.click("#menu-btn")   # крестик
        await wait_closed(page)
        check(not (await state(page))["open"], "крестик закрывает", failures)

        await page.click("#menu-btn")
        await page.wait_for_timeout(350)
        await page.mouse.click(10, 400)   # затемнение слева от панели
        await wait_closed(page)
        check(not (await state(page))["open"], "клик по затемнению закрывает", failures)

        for name, tab in [("Новости", "news"), ("Документы", "docs"), ("Сервисы", "services"), ("Раздел Департамента ДОУ", "dou")]:
            await page.click("#menu-btn")
            await page.wait_for_timeout(350)
            await page.locator("#site-menu nav a", has_text=name).click()
            await wait_closed(page)
            ok = await page.evaluate(f"location.hash === '#{tab}' && !document.getElementById('tab-{tab}').hidden")
            act = await page.evaluate("document.querySelector('header nav a.active')?.dataset.tab")
            check(ok and act == tab and not (await state(page))["open"], f"«{name}» открывает вкладку и закрывает меню", failures)

        await page.click("#menu-btn")
        await page.wait_for_timeout(350)
        await page.click("#theme")
        await page.wait_for_timeout(200)
        theme = await page.evaluate("document.documentElement.dataset.theme")
        check((await state(page))["open"] and theme == "light", "смена темы в меню, меню остаётся открытым", failures)
        await page.click("#theme")
        await page.click("#me-name")
        await page.wait_for_timeout(300)
        check(await page.locator("#me-dlg[open]").count() == 1 and not (await state(page))["open"],
              "«Мой профиль» открывает окно профиля и закрывает меню", failures)
        await page.click("#me-cancel")

        await page.click("#menu-btn")
        await page.wait_for_timeout(350)
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.wait_for_timeout(300)
        s = await state(page)
        check(not s["compact"] and not s["open"] and s["overflow"] != "hidden", "переход на десктопную ширину закрывает меню",
              failures)

        print("Десктоп 1280×800")
        check(await page.locator("#menu-btn").is_hidden() and await page.locator("header nav").is_visible()
              and await page.locator("#logout").is_visible(), "кнопки меню нет, вкладки и «Выйти» в шапке", failures)
        hdr = await page.locator("header").bounding_box()
        check(hdr["height"] <= 58, f"шапка в одну строку ({hdr['height']:.0f} px)", failures)

        print("Без анимаций (prefers-reduced-motion)")
        await page.emulate_media(reduced_motion="reduce")
        await page.set_viewport_size({"width": 390, "height": 844})
        dur = await page.evaluate("getComputedStyle(document.getElementById('site-menu')).transitionDuration")
        check(all(float(x.strip().rstrip("s")) == 0 for x in dur.split(",")), f"переходы отключены ({dur})", failures)
        await page.emulate_media(reduced_motion="no-preference")

        print("Администратор")
        actx = await browser.new_context(viewport={"width": 390, "height": 844}, locale="ru-RU")
        actx.on("request", lambda r: external.append(r.url) if not LOCAL.match(r.url) else None)
        apage = await login(actx, url, admin, admin_password)
        await open_tab(apage, url, "admin", "dark")
        await apage.click("#menu-btn")
        await apage.wait_for_timeout(350)
        s = await state(apage)
        check(s["items"][-1] == "Админ-панель" and len(s["items"]) == 5, "администратору — пять вкладок с «Админ-панелью»", failures)
        act = await apage.evaluate("getComputedStyle(document.querySelector('#site-menu nav a.active')).boxShadow")
        check("inset" in act, "активная вкладка выделена полоской слева", failures)
        if shots:
            await apage.mouse.move(5, 300)
            await apage.screenshot(path=str(SHOTS / "mobile_menu_admin.png"))
        await apage.keyboard.press("Escape")
        await wait_closed(apage)
        check(not (await state(apage))["hscroll"], "админ-панель на телефоне без горизонтальной прокрутки", failures)

        print("Страница входа")
        lctx = await browser.new_context(viewport={"width": 360, "height": 740}, locale="ru-RU")
        lctx.on("request", lambda r: external.append(r.url) if not LOCAL.match(r.url) else None)
        lpage = await lctx.new_page()
        await lpage.goto(url + "/login", wait_until="networkidle")
        cut = await lpage.evaluate("(t => t.getBoundingClientRect().right <= document.getElementById('theme').getBoundingClientRect().left)"
                                   "(document.querySelector('header .title'))")
        check(cut and not await lpage.evaluate("document.documentElement.scrollWidth > innerWidth"),
              "вход 360 px: название не наезжает на кнопку темы, нет горизонтальной прокрутки", failures)

        check(not external, f"нет запросов к внешним хостам (найдено: {external[:3]})", failures)
        await browser.close()
    print("\nИТОГ:", "все проверки пройдены" if not failures else f"провалено {len(failures)}: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--login", default="sotrudnik")
    ap.add_argument("--admin-login", default="admin")
    ap.add_argument("--shots", action="store_true", help="снять docs/screenshots/mobile_*.png")
    a = ap.parse_args()
    pw, apw = os.environ.get("DEMO_PASSWORD"), os.environ.get("ADMIN_PASSWORD")
    if not pw or not apw:
        sys.exit("Нужны DEMO_PASSWORD (сотрудник) и ADMIN_PASSWORD (администратор)")
    sys.exit(asyncio.run(main(a.url.rstrip("/"), a.login, pw, a.admin_login, apw, a.shots)))
