"""Живая речь: сленг, опечатки, раскладка, вежливые реплики, продолжение темы, заполнение шаблона своими словами.
Запуск: .venv/bin/pytest -q tests/test_dialog.py (диалоги с поиском пропускаются без Ollama)."""
from __future__ import annotations

from collections import Counter

import pytest

from app.dialog import intents
from app.dialog.normalize import expand, fix_layout, normalize, substitute


def test_slang_and_courtesy():
    cases = {
        "скока дней на согласование": "сколько дней на согласование",
        "когда регать входящие": "когда регистрировать входящие",
        "бумажка пришла, а она уже зарегана в электронке":
            "бумажный документ пришла, а она уже зарегистрирована в электронной форме",
        "подскажи плиз как оформить гриф согласования": "как оформить гриф согласования",
        "Слушайте, подскажите пожалуйста, кто визирует приказ? Заранее спасибо!": "кто визирует приказ?",
        "спасибо, а если вечером пришло?": "а если вечером пришло?",
        "СЭД лежит, как регать документы?": "ИИС ДО лежит, как регистрировать документы?",
    }
    for raw, want in cases.items():
        assert normalize(raw).text == want, raw


def test_smalltalk_kinds():
    cases = {"спасибо": "thanks", "спс, понял": "thanks", "спасибо большое!": "thanks", "ок": "ack",
             "хорошо, понял": "ack", "👍": "ack", "пока": "bye", "как дела?": "how", "ты бот?": "who",
             "ты кто вообще": "who", "не понял": "confused", "объясни проще": "confused", "это не то": "wrong",
             "отмена": "cancel", "да": "yes", "давай": "yes", "нет": "no", "привет": "greeting",
             "что ты умеешь?": "help"}
    for raw, kind in cases.items():
        assert normalize(raw).smalltalk == kind, raw
    for raw in ["спасибо, а если вечером пришло?", "привет, когда регистрировать входящие?", "письмо пришло не по адресу",
                "да, а приказ?"]:
        assert normalize(raw).smalltalk is None, raw


def test_typos_only_for_unknown_words():
    vocab = Counter({"согласование": 5, "регистрация": 3, "распоряжение": 4, "подготовить": 2, "документ": 9})
    n = normalize("скока дней на согласавание распоражения", vocab)
    assert n.text == "сколько дней на согласование распоряжения"
    assert n.fixes == [("согласавание", "согласование"), ("распоражения", "распоряжения")]
    # Настоящие слова не «исправляются» в слова базы знаний: «приготовить» ≠ «подготовить»
    for raw in ["Как приготовить борщ?", "Сколько стоит парковка у офиса?", "Громов Иванов"]:
        assert normalize(raw, vocab).fixes == [], raw


def test_keyboard_layout():
    assert fix_layout("rjulf htubcnhbhjdfnm d[jlzobq ljrevtyn") == "когда регистрировать входящий документ"
    for raw in ["help", "/help", "Wi-Fi пароль", "internal-6", "disp_working_group"]:
        assert fix_layout(raw) is None, raw


def test_followup_substitution():
    assert substitute("Когда регистрировать входящий документ?", "а исходящий?") == \
        "Когда регистрировать исходящий документ?"
    assert substitute("Сколько дней на согласование распоряжения?", "а для приказа?") == \
        "Сколько дней на согласование приказа?"
    assert substitute("Кто подписывает постановление Организации?", "а приказ?") == "Кто подписывает приказ Организации?"
    # прилагательное согласуется с заменяемым: «исходящее письмо», а не «исходящий письмо»
    assert substitute("Когда регать входящее письмо?", "а исходящий?") == "Когда регать исходящее письмо?"
    # «в выходной» — новое обстоятельство, а не замена «входящий»; два новых слова — тоже не замена
    assert substitute("Когда регистрировать входящий документ?", "а в выходной?") is None
    assert substitute("Когда регистрировать входящий документ?", "а если вечером пришло?") is None


def test_colloquial_terms_expand_search():
    assert "некорректная маршрутизация" in expand("мне принесли чужое письмо, что делать")
    assert "некорректная маршрутизация" in expand("письмо пришло не по адресу")
    assert "обнаружение денег" in expand("в конверте лежали деньги")
    assert expand("как получить деньги на командировку") == "как получить деньги на командировку"
    assert "отсутствии подписанта" in expand("можно ли подписаться за руководитель, если он в отпуске?")
    assert "отсутствии подписанта" in expand("руководитель на больничном, кто подпишет приказ?")
    assert expand("когда оформлять приказ об отпуске") == "когда оформлять приказ об отпуске"


def test_classify_colloquial():
    class Llm:   # маленькая модель на «письмо пришло не по адресу» отвечала check_doc
        calls = 0

        def chat(self, *a, **k):
            Llm.calls += 1
            return "check_doc"

    assert intents.classify("письмо пришло не по адресу", llm=Llm()) == ("question", "default")
    assert Llm.calls == 0   # без слов намерения модель не спрашиваем
    assert intents.classify("мне принесли чужое письмо, что с ним делать")[0] == "question"
    assert intents.classify("кому нести на визу приказ про выходные")[0] == "approval"
    assert intents.classify(normalize("скока дней на согласование приказа").text)[0] == "deadline"
    assert intents.classify("а исходящий?") == ("question", "mark")
    assert intents.classify("а когда регистрировать исходящий?") == ("question", "rule")


def test_guard_allows_latin_from_fragments():
    from app.llm.guard import check_explanation
    frag = ["Основной текст документа оформляется шрифтом Times New Roman размером 13 пунктов."]
    assert check_explanation("Текст документа оформляется шрифтом Times New Roman размером 13 пунктов.", frag)[0]
    assert not check_explanation("Текст документа оформляется шрифтом Arial размером 13 пунктов.", frag)[0]


def test_smalltalk_replies_without_refusal():
    from app.dialog.engine import Session, get_engine
    eng = get_engine()
    for text in ["спасибо", "ок", "пока", "как дела?", "ты бот?", "отмена", "да", "ты тупой"]:
        r = eng.handle(Session(), text)
        assert r.intent == "smalltalk" and not r.refused and "недостаточно информации" not in r.text, text
    r = eng.handle(Session(), "не понял")
    assert r.intent == "smalltalk" and "своими словами" in r.text


def test_fill_understands_live_speech():
    from app.dialog.engine import Session, get_engine
    eng = get_engine()
    s = Session(user="tester")
    r = eng.begin_fill(s, "disp_working_group")
    assert "Вопрос 1 из" in r.text
    r = eng.handle(s, "не знаю")
    assert "Подскажу" in r.text and "Вопрос 1 из" in r.text and "subject" not in s.fill.values
    r = eng.handle(s, "спасибо")
    assert r.text.startswith("Пожалуйста! Продолжим.") and "Вопрос 1 из" in r.text
    r = eng.handle(s, "нет")
    assert "обязательный" in r.text and "subject" not in s.fill.values
    r = eng.handle(s, "подготовке проекта Регламента электронного архива")
    assert s.fill.values["subject"] == "подготовке проекта Регламента электронного архива" and "Вопрос 2 из" in r.text
    r = eng.handle(s, "передумал")
    assert s.fill is None and "прервано" in r.text


def _ollama_up() -> bool:
    try:
        from app.llm.client import get_client
        return get_client().health().get("ok", False)
    except Exception:  # noqa: BLE001
        return False


needs_ollama = pytest.mark.skipif(not _ollama_up(), reason="Ollama недоступна")


@needs_ollama
def test_dialog_keeps_topic():
    from app.dialog.engine import Session, get_engine
    eng = get_engine()
    s = Session()
    eng.handle(s, "Когда регистрировать входящий документ?")
    r = eng.handle(s, "а исходящий?")
    assert r.sources[0].section == "7.1" and "Понял вопрос так: «Когда регистрировать исходящий документ?»" in r.text
    s = Session()
    eng.handle(s, "Кто визирует распоряжение о создании рабочей группы?")
    r = eng.handle(s, "а приказ о выходных?")
    assert r.intent == "approval" and r.meta["row"] == "internal-1"
    s = Session()
    eng.handle(s, "Сколько дней на согласование распоряжения?")
    r = eng.handle(s, "а для приказа?")
    assert r.intent == "deadline" and "10 рабочих дней" in r.text


@needs_ollama
def test_colloquial_questions_find_points():
    from app.dialog.engine import Session, get_engine
    eng = get_engine()
    for text, sec in [("письмо пришло не по адресу", "5.14"), ("мне принесли чужое письмо, че делать", "5.14"),
                      ("в конверте лежали деньги", "5.4.3"), ("когда регать входящие", "5.6")]:
        r = eng.handle(Session(), text)
        assert not r.refused and r.sources[0].section == sec, text
    r = eng.handle(Session(), "скока дней на согласавание приказа")
    assert r.intent == "deadline" and "10 рабочих дней" in r.text and "«согласавание» → «согласование»" in r.text


@needs_ollama
def test_offer_template_after_answer_and_side_question():
    from app.dialog.engine import Session, get_engine
    eng = get_engine()
    s = Session(user="tester")
    r = eng.handle(s, "Что должно быть указано в приказе о вводе в эксплуатацию информационной системы?")
    assert any("Подготовить черновик" in a for a, _ in r.actions)
    r = eng.handle(s, "да")
    assert r.meta.get("template") == "order_is_commissioning" and s.fill is not None
    r = eng.handle(s, "а с кем его потом согласовывать?")
    assert "Департамент информационной безопасности" in r.text and "Продолжим заполнение" in r.text
    assert "Могу сразу подготовить" not in r.text and s.fill is not None and not s.fill.values
