"""Duplicate detection for obligations created from text.

One rule for both places that need it: the translation of a proposal (to ask the owner
before a preview exists) and the transaction that applies a confirmed preview (the data may
have changed in between, and one batch may contain the same name twice)."""

from __future__ import annotations

from sqlalchemy import select

from xboct.application.uow import Uow
from xboct.storage.models import Obligation


def same_name(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def is_duplicate(new_title: str, ob: Obligation) -> bool:
    """Same name, a name that equals an alias, or one name extending the other by a word."""
    wanted = same_name(new_title)
    for name in (same_name(ob.title), *(same_name(a) for a in ob.aliases)):
        if name == wanted or name.startswith(wanted + " ") or wanted.startswith(name + " "):
            return True
    return False


async def find_duplicate(uow: Uow, title: str) -> Obligation | None:
    """An active obligation (including one created earlier in this transaction)."""
    rows = await uow.session.scalars(
        select(Obligation).where(Obligation.active).order_by(Obligation.created_at)
    )
    return next((ob for ob in rows if is_duplicate(title, ob)), None)
