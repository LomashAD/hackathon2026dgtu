"""Калькулятор сроков в датах: производственный календарь, сложение рабочих дней, разбор даты из текста,
сроки из текста пунктов и диалог «когда будет готово?». Ollama не нужна: расчёт делает код, «сегодня» подменяется.
Запуск: .venv/bin/pytest -q tests/test_deadlines.py"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path

import pytest
import yaml

from app.approval import deadlines as d
from app.dialog import intents

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "data" / "Инструкция_по_ДОУ_учебный_пример.pdf"
D = dt.date
TODAY = D(2026, 10, 24)   # суббота


@pytest.fixture(scope="module")
def cal():
    return d.parse_calendar(yaml.safe_load((ROOT / "app" / "approval" / "calendar_ru.yaml").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def sections():
    from app.ingest.parsers import parse_instruction_pdf
    return {s["section_path"]: s["text"] for s in parse_instruction_pdf(PDF)}


@pytest.fixture
def frozen(monkeypatch):
    """«Сегодня» — суббота, 24.10.2026 (и в намерениях, и в движке)."""
    monkeypatch.setattr(d, "today", lambda: TODAY)


# ------------------------------------------------------------------ календарь и сложение дней

def test_calendar_file(cal):
    workdays = sum(cal.is_workday(D(2026, 1, 1) + dt.timedelta(days=i)) for i in range(365))
    assert workdays == 247                                  # официальная норма 2026 года: 247 рабочих дней
    assert cal.years == {2026: "confirmed", 2027: "preliminary"}
    assert cal.check[2026] and cal.check[2027]              # что требует проверки — записано в файле


def test_calendar_path_in_config():
    from app.config import get_config, path
    assert get_config()["paths"]["calendar"] == "app/approval/calendar_ru.yaml"
    assert path("calendar").exists()
    assert d.get_calendar().years                            # загружается по пути из config.yaml


def test_add_workdays_over_weekend(cal):
    assert d.add_workdays(D(2026, 10, 23), 1, cal) == D(2026, 10, 26)   # пт + 1 → пн
    assert d.add_workdays(D(2026, 11, 12), 3, cal) == D(2026, 11, 17)   # чт + 3 → вт
    assert d.add_workdays(D(2026, 11, 16), 10, cal) == D(2026, 11, 30)


def test_add_workdays_over_may_holidays(cal):
    assert d.add_workdays(D(2026, 4, 30), 3, cal) == D(2026, 5, 6)      # 1–3 мая не считаются
    assert d.add_workdays(D(2026, 5, 6), 10, cal) == D(2026, 5, 21)     # 9–11 мая не считаются
    assert d.add_workdays(D(2026, 10, 27), 10, cal) == D(2026, 11, 11)  # 4 ноября не считается


def test_add_workdays_over_new_year(cal):
    assert d.add_workdays(D(2026, 12, 29), 1, cal) == D(2026, 12, 30)
    assert d.add_workdays(D(2026, 12, 30), 1, cal) == D(2027, 1, 11)    # 31.12 (перенос) и 1–10 января
    assert d.add_workdays(D(2026, 12, 30), 10, cal) == D(2027, 1, 22)
    assert d.add_workdays(D(2025, 12, 31), 1, cal) == D(2026, 1, 12)     # каникулы 1–11 января 2026


def test_transferred_days(cal):
    for day in [D(2026, 1, 9), D(2026, 12, 31)]:             # переносы с 3 и 4 января
        assert not cal.is_workday(day)
    assert not cal.is_workday(D(2026, 3, 9))                 # 8 марта в воскресенье → выходной 9 марта
    assert not cal.is_workday(D(2026, 5, 11))                # 9 мая в субботу → выходной 11 мая
    assert d.add_workdays(D(2026, 1, 8), 1, cal) == D(2026, 1, 12)
    # рабочая суббота по переносу считается рабочим днём
    sat = d.parse_calendar({"years": {2026: {"work": [D(2026, 11, 7)]}}})
    assert sat.is_workday(D(2026, 11, 7)) and d.add_workdays(D(2026, 11, 6), 1, sat) == D(2026, 11, 7)


def test_short_days_are_full_workdays(cal):
    for day in [D(2026, 4, 30), D(2026, 5, 8), D(2026, 6, 11), D(2026, 11, 3)]:
        assert day in cal.short and cal.is_workday(day)
    assert d.add_workdays(D(2026, 4, 29), 1, cal) == D(2026, 4, 30)     # сокращённый — полный день срока
    assert d.add_workdays(D(2026, 11, 2), 1, cal) == D(2026, 11, 3)


def test_send_on_weekend_counts_from_next_workday(cal):
    assert d.add_workdays(D(2026, 10, 24), 3, cal) == D(2026, 10, 28)   # сб → отсчёт с пн: пн, вт, ср
    assert d.add_workdays(D(2026, 10, 25), 3, cal) == D(2026, 10, 28)   # вс — то же
    assert d.add_workdays(D(2026, 5, 9), 1, cal) == D(2026, 5, 12)      # праздник → ближайший рабочий


def test_zero_and_one_day(cal):
    assert d.add_workdays(D(2026, 10, 23), 0, cal) == D(2026, 10, 23)   # рабочий день — он сам
    assert d.add_workdays(D(2026, 10, 24), 0, cal) == D(2026, 10, 26)   # выходной — ближайший рабочий
    assert d.add_workdays(D(2026, 10, 22), 1, cal) == D(2026, 10, 23)
    assert d.add_workdays(D(2026, 10, 24), 1, cal) == D(2026, 10, 26)
    with pytest.raises(ValueError):
        d.add_workdays(D(2026, 10, 22), -1, cal)


def test_calendar_days_end_on_previous_workday(cal):
    # п. 6.4: последний день срока на выходном — срок завершается в предшествующий рабочий день
    assert d.add_calendar_days(D(2026, 11, 12), 30, cal) == D(2026, 12, 11)
    assert d.add_calendar_days(D(2026, 11, 12), 30, cal, shift="next") == D(2026, 12, 14)
    assert d.add_calendar_days(D(2026, 10, 1), 30, cal) == D(2026, 10, 30)  # 31.10 — суббота


def test_outside_calendar_only_weekends(cal):
    assert cal.gaps(D(2030, 12, 25), D(2031, 1, 10)) == ([2030, 2031], [])
    assert cal.gaps(D(2026, 12, 30), D(2027, 1, 22)) == ([], [2027])
    assert cal.is_workday(D(2030, 1, 1))                     # праздника 2030 года в календаре нет — только выходные
    assert not cal.is_workday(D(2030, 1, 5))                 # суббота
    empty = d.parse_calendar({})
    assert d.add_workdays(D(2026, 5, 6), 10, empty) == D(2026, 5, 20)   # без файла — только выходные


# ------------------------------------------------------------------ дата из текста

@pytest.mark.parametrize("text, want", [
    ("сегодня", D(2026, 10, 24)),
    ("завтра", D(2026, 10, 25)),
    ("послезавтра", D(2026, 10, 26)),
    ("во вторник", D(2026, 10, 27)),
    ("отправил во вторник", D(2026, 10, 20)),          # прошедшее время — прошедший вторник
    ("в следующую пятницу", D(2026, 10, 30)),
    ("в пт", D(2026, 10, 30)),
    ("15 ноября", D(2026, 11, 15)),
    ("15.11", D(2026, 11, 15)),
    ("15.11.2026", D(2026, 11, 15)),
    ("15.11.26", D(2026, 11, 15)),
    ("2026-11-15", D(2026, 11, 15)),
    ("12 ноября 2026", D(2026, 11, 12)),
    ("12-го ноября 2026 года", D(2026, 11, 12)),
    ("отправлю 12 января", D(2027, 1, 12)),             # без года — ближайшая к сегодняшней дата
    ("севодня", D(2026, 10, 24)),                       # опечатки
    ("зафтра", D(2026, 10, 25)),
    ("в втроник", D(2026, 10, 27)),
    ("12 нояьря", D(2026, 11, 12)),
    ("в пятнцу", D(2026, 10, 30)),
    ("по п. 10.11 до 15 ноября", D(2026, 11, 15)),      # номер пункта — не дата
])
def test_parse_date(text, want):
    r = d.parse_date_from_text(text, TODAY)
    assert r is not None and r.date == want, text


@pytest.mark.parametrize("text", ["31.02", "в течение 10 рабочих дней", "ред. 1.0", "п. 10.11", "среди документов",
                                  "срок 3 рабочих дня", "как приготовить борщ"])
def test_parse_date_none(text):
    assert d.parse_date_from_text(text, TODAY) is None


# ------------------------------------------------------------------ сроки и порядок — из текста пунктов

def test_terms_from_instruction(sections):
    items = d.parse_items(sections["10.3"])
    assert d.parse_term(d.match_doc(items, "распоряжение")[1]) == (3, "work")
    assert d.match_doc(items, "проект приказа")[0] == "приказ по основной деятельности"   # не «проект модуля»
    assert d.match_doc(items, "сколько дней положено") is None                          # не «положение»
    assert d.match_doc(items, "другой документ")[0].startswith("иной")
    assert d.match_doc(items, "отправлю документ сегодня") is None                     # вид не назван — уточнить
    assert d.match_doc(items, "аналитический документ")[0].startswith("аналитический")
    assert d.parse_term(d.match_sender(d.parse_items(sections["6.6"]), "приказ")[1]) == (30, "calendar")
    assert d.find_term(sections["10.15"]) == (3, "work", True)          # «как правило, не более чем за три рабочих дня»
    assert d.find_term(sections["10.6"]) is None                        # у визирования срока нет
    assert d.find_term(sections["10.13"]) is None                       # у лингвистической экспертизы — тоже
    assert d.urgent_term(sections["6.7"], very=False) == (3, "work")
    assert d.urgent_term(sections["6.7"], very=True) == (1, "work")
    assert d.end_shift(sections["6.4"]) == "prev"


def test_review_mode_from_text(sections):
    assert d.review_mode(sections["10.2"]) == "parallel"                # «одновременно направляет ПД во все…»
    seq = sections["10.2"].replace("одновременно направляет", "поочередно направляет")
    assert d.review_mode(seq) == "sequential"
    assert d.review_mode(sections["10.2"].replace("одновременно ", "")) == "unknown"


def test_new_edition_changes_terms(sections):
    """Числа сроков не зашиты в код: другая редакция п. 10.3 — другой срок."""
    edited = sections["10.3"].replace("распоряжение — 3 рабочих дня", "распоряжение — 7 рабочих дней")
    assert d.parse_term(d.match_doc(d.parse_items(edited), "распоряжение")[1]) == (7, "work")


def test_build_schedule(cal):
    steps = [d.Step("А", "юр", 3, after="start"), d.Step("Б", "ДОУ", 3), d.Step("В", "подписант", 3, after="later"),
             d.Step("Г", "?", None)]
    rows = d.build_schedule(D(2026, 11, 12), steps, cal)
    assert [r.due for r in rows] == [D(2026, 11, 17), D(2026, 11, 20), None, None]
    assert rows[1].start == D(2026, 11, 17)


def test_days_word():
    assert [d.days_word(n) for n in (1, 3, 5, 11, 21)] == [
        "1 рабочий день", "3 рабочих дня", "5 рабочих дней", "11 рабочих дней", "21 рабочий день"]
    assert d.days_word(30, "calendar") == "30 календарных дней"


# ------------------------------------------------------------------ намерение

@pytest.mark.parametrize("text", [
    "Если отправлю распоряжение на согласование сегодня, когда будет готово?",
    "К какому числу согласуют приказ, если отправить во вторник?",
    "Когда истекает срок рассмотрения, если отправил 12 ноября?",
    "до какого числа надо ответить гражданину, если обращение пришло 12 ноября",
    "отправил приказ в пятницу, когда согласуют?",
    "через сколько дней согласуют распоряжение если отправить 15.11",
    "какой крайний срок рассмотрения приказа?",
])
def test_classify_deadline_calc(frozen, text):
    assert intents.classify(text) == ("deadline_calc", "rule")


@pytest.mark.parametrize("text, intent", [
    ("Сколько дней на согласование распоряжения?", "deadline"),               # срок в днях, без даты
    ("Через сколько дней после подписания регистрируют исходящий документ?", "question"),
    ("Если в поручении написано «до 14 октября», 14 октября входит в срок?", "question"),
    ("срок выпал на субботу — когда сдавать?", "question"),
    ("Пришел документ из министерства с пометкой «Весьма срочно», когда его надо исполнить?", "question"),
    ("Кто визирует приказ о выходных?", "approval"),
])
def test_classify_keeps_old_intents(frozen, text, intent):
    assert intents.classify(text)[0] == intent


# ------------------------------------------------------------------ диалог

@pytest.fixture
def eng(frozen):
    from app.dialog.engine import get_engine
    return get_engine()


def _ask(eng, *turns, role="employee"):
    from app.dialog.engine import Session
    s = Session(user="tester", role=role)
    for t in turns:
        r = eng.handle(s, t)
    return s, r


def test_dialog_full_request(eng):
    _, r = _ask(eng, "Если отправлю распоряжение на согласование 12 ноября 2026, когда будет готово?")
    assert r.intent == "deadline_calc" and not r.refused and not r.meta.get("clarification")
    assert r.text.startswith("**Итог: рассмотрение завершится не позднее вт, 17.11.2026**")
    assert "| Этап | Кто | Срок, раб. дн. | Крайняя дата |" in r.text
    assert "Юридическое подразделение, Департамент ДОУ" in r.text            # кто — из Матрицы, строка 7
    assert "**Основание:** Инструкция по ДОУ, ред. 1.0" in r.text and "**Простыми словами:**" in r.text
    got = [f"{x.doc_id}:{x.section}" for x in r.sources]
    assert {"instr_dou:10.2", "instr_dou:10.3"} <= set(got) and any(x.kind == "matrix" for x in r.sources)
    assert r.text.index("**Итог") < r.text.index("| Этап") < r.text.index("**Основание") < r.text.index("**Простыми")


def test_dialog_today_from_system(eng):
    _, r = _ask(eng, "Если отправлю распоряжение на согласование сегодня, когда будет готово?")
    assert "сб, 24.10.2026" in r.text and "ср, 28.10.2026" in r.text      # отсчёт с понедельника
    assert "нерабочий день, отсчёт начинается с ближайшего рабочего дня" in r.text


def test_dialog_asks_document_type(eng):
    s, r = _ask(eng, "Когда истекает срок рассмотрения, если отправил 12 ноября?")
    assert r.meta.get("clarification") and r.intent == "deadline_calc" and "какой документ" in r.text
    assert ("Распоряжение", "распоряжение") in r.actions
    r = eng.handle(s, "распоряжение")
    assert not r.meta.get("clarification") and "вт, 17.11.2026" in r.text


def test_dialog_asks_date(eng):
    s, r = _ask(eng, "Когда будет готово, если отправлю приказ на согласование?")
    assert r.meta.get("clarification") and "когда документ отправлен" in r.text
    assert [a for a, _ in r.actions][:2] == ["Сегодня (сб, 24.10.2026)", "Завтра (вс, 25.10.2026)"]
    r = eng.handle(s, "во вторник")
    assert "вт, 27.10.2026" in r.text and "ср, 11.11.2026" in r.text      # 10 рабочих дней, 4 ноября не считается


def test_dialog_one_question_for_both(eng):
    s, r = _ask(eng, "Когда будет готово согласование?")
    assert r.meta.get("clarification") and "какой документ" in r.text and "когда его отправили" in r.text
    r = eng.handle(s, "письмо, сегодня")
    assert not r.meta.get("clarification") and "ср, 28.10.2026" in r.text
    assert "зависят от адресата письма" in r.text


def test_dialog_followup_other_document(eng):
    s, r = _ask(eng, "К какому числу согласуют распоряжение, если отправить 6 мая 2026?")
    assert "вт, 12.05.2026" in r.text                                       # 7, 8, 12 мая; 9–11 мая не считаются
    r = eng.handle(s, "а если приказ?")
    assert r.intent == "deadline_calc" and "ср, 06.05.2026" in r.text and "чт, 21.05.2026" in r.text
    r = eng.handle(s, "а если в пятницу?")
    assert "пт, 30.10.2026" in r.text and "приказ по основной деятельности" in r.text


def test_dialog_only_term_button(eng):
    s, r = _ask(eng, "Когда будет готово, если отправлю приказ на согласование?")
    r = eng.handle(s, "только срок")
    assert r.intent == "deadline" and "10 рабочих дней" in r.text            # прежний ответ — срок в днях


def test_dialog_no_term_honest_refusal(eng):
    _, r = _ask(eng, "Когда истекает срок визирования приказа, если отправил 12 ноября 2026?")
    assert r.refused and "**Срок визирования в базе знаний не указан**" in r.text
    assert "**Куда обратиться:** Департамент документационного обеспечения" in r.text
    assert not re.search(r"\d{2}\.\d{2}\.\d{4}", r.text)                  # дат не выдумывает
    _, r = _ask(eng, "До какого числа пройдёт лингвистическая экспертиза, если отправить приказ завтра?")
    assert r.refused and "лингвистической экспертизы в базе знаний не указан" in r.text


def test_dialog_out_of_scope_not_calc(eng):
    """Не о документах — не калькулятор: обычный ответ по базе знаний (с отказом), а не вопрос о виде документа."""
    from app.dialog.engine import Session
    assert eng.deadline_calc(Session(), "Когда будет готова справка о зарплате, если подам заявление сегодня?") is None


def test_dialog_execution_and_signing(eng):
    _, r = _ask(eng, "до какого числа надо ответить на обращение гражданина, если его зарегистрировали 12 ноября 2026")
    assert "пт, 11.12.2026" in r.text and "сб, 12.12.2026 — это нерабочий день" in r.text
    assert {"instr_dou:6.6", "instr_dou:6.4"} <= {f"{x.doc_id}:{x.section}" for x in r.sources}
    _, r = _ask(eng, "Когда подпишут, если направил подписанту 30 апреля 2026?")
    assert "ср, 06.05.2026" in r.text and "как правило" in r.text


def test_dialog_calendar_warnings(eng):
    _, r = _ask(eng, "К какому числу согласуют распоряжение, если отправить 12 ноября 2030?")
    assert "праздничные дни за этот период не учтены" in r.text.lower()
    _, r = _ask(eng, "К какому числу согласуют приказ, если отправить 30.12.2026?")
    assert "пт, 22.01.2027" in r.text and "Календарь на 2027 г. предварительный" in r.text
    _, r = _ask(eng, "Если отправлю распоряжение на согласование 27 апреля 2026, когда будет готово?")
    assert "чт, 30.04.2026 — предпраздничный день" in r.text


def test_dialog_both_variants_when_order_unknown(eng, monkeypatch):
    """Если в п. 10.2 не сказано «одновременно», расчёт не угадывает: показывает оба варианта и помечает это."""
    orig = eng.retriever.get_section

    def patched(doc_id, section, role):
        hit = orig(doc_id, section, role)
        if hit and section == "10.2":
            hit = dataclasses.replace(hit, text=hit.text.replace("одновременно ", ""))
        return hit

    monkeypatch.setattr(eng.retriever, "get_section", patched)
    _, r = _ask(eng, "К какому числу согласуют приказ, если отправить 12 ноября 2026?")
    assert "показаны оба варианта" in r.text and "если по очереди" in r.text
    assert r.meta["calc"]["due"] == ["2026-11-26", "2026-12-10"]             # 10 дней сразу; 10 + 10 по очереди


def test_dialog_closed_points_not_in_sources(eng, monkeypatch):
    """Пункт, закрытый для роли, не попадает ни в источники, ни в текст ответа."""
    orig = eng.retriever.get_section
    monkeypatch.setattr(eng.retriever, "get_section",
                        lambda doc_id, section, role: None if section == "10.15" and role == "employee"
                        else orig(doc_id, section, role))
    _, r = _ask(eng, "К какому числу согласуют приказ, если отправить 12 ноября 2026?")
    assert all(x.section != "10.15" for x in r.sources) and "10.15" not in r.text
    _, r = _ask(eng, "К какому числу согласуют приказ, если отправить 12 ноября 2026?", role="admin")
    assert any(x.section == "10.15" for x in r.sources)


def test_dialog_audit_masked(eng, monkeypatch):
    from app.security import audit
    rec = []
    monkeypatch.setattr(audit, "_write", lambda key, record: rec.append((key, record)))
    _, r = _ask(eng, "Иванов Иван Иванович отправил приказ 12 ноября 2026, когда будет готово?")
    logged = [x for k, x in rec if k == "audit_log"][-1]
    assert logged["intent"] == "deadline_calc" and "Иванов" not in logged["query"] and "[FIO]" in logged["query"]
    assert "instr_dou:10.3" in logged["sources"]


def test_dialog_offers_draft(eng):
    _, r = _ask(eng, "Распоряжение о создании рабочей группы отправлю на согласование 12 ноября 2026, когда будет готово?")
    assert any("Подготовить черновик" in a for a, _ in r.actions) and "«Внутренние документы», строка 6" in r.text
