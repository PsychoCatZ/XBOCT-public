"""Owner's style: «ты», short, natural. Detection only: stored data is never rewritten."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from xboct import style
from xboct.style import formal_markers

SRC = Path(style.__file__).parent


@pytest.mark.parametrize(
    "text",
    [
        "Уточните, пожалуйста, какой Demo",
        "Вы имеете в виду подписку?",
        "Сообщите вам удобное время",
        "Ваш платёж не найден",
        "Выберите запись",
        "Укажите дату",
        "Проверьте данные и попробуйте снова",
        "Вас понял",
    ],
)
def test_formal_wording_is_detected(text):
    assert formal_markers(text)


@pytest.mark.parametrize(
    "text",
    [
        "Какой Demo оплатил — подписку или API? Выбери.",
        "Уточни, какой период имеешь в виду.",
        "Укажи дату: пока она неизвестна.",
        "Привет, Кот ) Какие дела разберём?",
        "Выручил, вывод верный, навык хороший",  # «вы» inside other words
        "Повтори запрос, покажу актуальный список.",
        "Подожди 15 минут и запроси новый код.",
    ],
)
def test_the_owners_style_passes(text):
    assert formal_markers(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "На каком сайте оплатил?",  # «сайте» ends in -ите but is not an address
        "Оплатил на сайте и в приложении.",
        "В граните, в магните, в лайте.",
        "Что на сайте сегодня?",
        "Какой сайт нужен? Укажи.",
        "Платёж на сайте найден в первой очереди.",
        "Найди сайт и проверь, что там.",
        "Спроси в банке и в клубе.",
    ],
)
def test_neutral_words_with_the_same_endings_are_not_flagged(text):
    assert formal_markers(text) == []


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Укажите дату", ["Укажите"]),
        ("Выберите, какой Demo.", ["Выберите"]),
        ("Проверьте данные, попробуйте снова.", ["Проверьте", "попробуйте"]),
        ("Воспользуйтесь формой", ["Воспользуйтесь"]),
        ("Подскажите, на каком сайте оплатили?", ["Подскажите"]),  # «сайте» stays unflagged
    ],
)
def test_known_formal_imperatives_are_flagged_by_the_exact_form(text, found):
    assert formal_markers(text) == found


def test_the_detector_only_reports_and_never_rewrites():
    text = "Подскажите, на каком сайте оплатили?"
    assert formal_markers(text)
    assert text == "Подскажите, на каком сайте оплатили?"


def test_quoted_owner_data_is_not_judged_and_not_changed():
    text = "У «Спросить, как вы там, пожалуйста» нет срока. Укажи его."
    assert formal_markers(text) == []
    assert "вы" in text  # detection never rewrites anything
    assert formal_markers("«Вы» — название. Укажите срок.") == ["Укажите"]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_every_fixed_text_of_the_application_is_in_style():
    """Strings in code, templates and scripts: nothing formal. Docstrings are not texts."""
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        if path == Path(style.__file__) or "versions" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docs:
                    continue
                scanned += 1
                if found := formal_markers(node.value):
                    offenders.append(f"{path.name}:{node.lineno}: {found}")
    for path in sorted([*SRC.rglob("*.html"), *SRC.rglob("*.js")]):
        if found := formal_markers(path.read_text()):
            offenders.append(f"{path.name}: {found}")
    assert scanned > 500  # the scan really looked at the texts
    assert offenders == []
