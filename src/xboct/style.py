"""Wording rules for XBOCT's own phrases (owner's style: «ты», short and natural).

XBOCT writes to the owner as «ты»: no «Вы/Вам/Ваш», no «пожалуйста», no plural imperatives
(«укажите», «выберите» — по списку известных форм); «Уточни…», «Выбери…», «Укажи…» are the model.

This module only *detects* breaches, for tests and eval reports. It never rewrites text:
titles, notes, quotes and other stored data stay exactly as the owner wrote them (the
owner's own «вы» or «пожалуйста» are data). Quoted spans «…» are therefore skipped.
"""

from __future__ import annotations

import re

_QUOTED = re.compile(r"«[^»]*»")
_LETTER = "а-яёa-z"

# Plural/formal imperatives of the verbs a dialog actually uses. A suffix (-ите/-йте/-ьте) is
# not a sign of formality by itself («на сайте», «в граните»), so the rule is a list of known
# forms; a form that is missing is simply not flagged (add it here with a test).
_FORMAL_IMPERATIVES = frozenset(
    (
        "уточните",
        "укажите",
        "выберите",
        "введите",
        "проверьте",
        "повторите",
        "попробуйте",
        "подождите",
        "обновите",
        "откройте",
        "закройте",
        "запросите",
        "скажите",
        "подскажите",
        "напишите",
        "сообщите",
        "отправьте",
        "пришлите",
        "добавьте",
        "удалите",
        "выполните",
        "подтвердите",
        "отмените",
        "создайте",
        "сохраните",
        "назовите",
        "дайте",
        "посмотрите",
        "исправьте",
        "измените",
        "нажмите",
        "перейдите",
        "зайдите",
        "войдите",
        "сверьте",
        "пополните",
        "продлите",
        "оплатите",
        "отметьте",
        "поясните",
        "опишите",
        "перечислите",
        "оставьте",
        "верните",
        "возьмите",
        "запишите",
        "внесите",
        "уберите",
        "перенесите",
        "отложите",
        "напомните",
        "выгрузите",
        "загрузите",
        "воспользуйтесь",
        "обратитесь",
        "убедитесь",
        "дождитесь",
        "вернитесь",
        "продолжайте",
        "будьте",
    )
)
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("пожалуйста", re.compile(r"пожалуйста", re.I)),
    ("вы", re.compile(rf"(?<![{_LETTER}])вы(?![{_LETTER}])", re.I)),
    ("вам/вас", re.compile(rf"(?<![{_LETTER}])ва[мс](?![{_LETTER}])", re.I)),
    ("ваш", re.compile(rf"(?<![{_LETTER}])ваш[{_LETTER}]*", re.I)),
    (
        "imperative",
        re.compile(
            rf"(?<![{_LETTER}])(?:{'|'.join(sorted(_FORMAL_IMPERATIVES))})(?![{_LETTER}])", re.I
        ),
    ),
)


def formal_markers(text: str) -> list[str]:
    """Formal-address markers in `text` outside «quotes»; an empty list means in style."""
    own = _QUOTED.sub(" ", text)
    found: list[str] = []
    for _, pattern in _RULES:
        found += [m.group(0) for m in pattern.finditer(own)]
    return found
