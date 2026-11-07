"""Mini App initData: the bot-owner HMAC algorithm, checked with an independent signer.

The signer below follows Telegram's documentation (not the module under test): sorted
`key=value` lines without `hash`, secret = HMAC_SHA256("WebAppData", bot_token).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

import pytest

from xboct.web.tma import MAX_BODY_BYTES, InitDataError, validate_init_data

TOKEN = "123456:SYNTHETIC-TEST-TOKEN"
OWNER = 424242
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def hash_of(fields: dict[str, str], token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields) if k != "hash")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def init(
    *, user: str | None = None, age: timedelta = timedelta(0), token: str = TOKEN, **extra: str
) -> str:
    fields = {
        "auth_date": str(int((NOW - age).timestamp())),
        "user": user if user is not None else json.dumps({"id": OWNER, "first_name": "Тест"}),
        "query_id": "AAHsynthetic",
        "signature": "c2lnbmF0dXJl",
        **extra,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    fields["hash"] = hash_of(fields, token)
    return urlencode(fields)


def check(raw: str) -> str:
    try:
        validate_init_data(raw, TOKEN, OWNER, NOW)
    except InitDataError as exc:
        return exc.code
    return "ok"


def test_a_valid_signature_of_the_owner_passes():
    user = validate_init_data(init(), TOKEN, OWNER, NOW)
    assert user.id == OWNER
    assert user.auth_date == NOW


def test_the_signature_field_stays_in_the_signed_text():
    # Only `hash` is left out; the third-party Ed25519 `signature` is just another field here.
    good = init()
    assert check(good) == "ok"
    tampered = good.replace("c2lnbmF0dXJl", "c2lnbmF0dXJm")
    assert check(tampered) == "signature"


def test_values_are_the_decoded_originals_not_a_reserialised_user():
    # `\/` and non-ASCII exactly as Telegram sends them; re-dumping the JSON would change them.
    raw_user = '{"id":424242,"first_name":"Тест\\/x","username":"a+b"}'
    assert check(init(user=raw_user)) == "ok"
    assert check(init(user=raw_user, extra_key="a b+c%d")) == "ok"


def test_percent_encoding_variants_decode_the_same():
    fields = {"auth_date": str(int(NOW.timestamp())), "user": json.dumps({"id": OWNER})}
    fields["hash"] = hash_of(fields)
    raw = "&".join(f"{k}={quote(v, safe='')}" for k, v in fields.items())
    assert check(raw) == "ok"


def test_wrong_token_is_a_signature_failure():
    assert check(init(token="999999:OTHER")) == "signature"


def test_a_field_changed_after_signing_is_a_signature_failure():
    # Signed for another account, then the user field is swapped for the owner's.
    fields = {"auth_date": str(int(NOW.timestamp())), "user": json.dumps({"id": 1})}
    fields["hash"] = hash_of(fields)
    fields["user"] = json.dumps({"id": OWNER})
    assert check(urlencode(fields)) == "signature"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "garbage",
        "a=1&&b=2",
        "auth_date=1&user={}",  # no hash
        "hash=" + "0" * 64,  # nothing else
    ],
)
def test_structurally_wrong_input_is_malformed(raw):
    assert check(raw) == "malformed"


def test_duplicate_keys_are_refused_even_with_a_valid_looking_hash():
    fields = {"auth_date": str(int(NOW.timestamp())), "user": json.dumps({"id": OWNER})}
    base = urlencode({**fields, "hash": hash_of(fields)})
    assert check(base + "&auth_date=" + fields["auth_date"]) == "malformed"
    assert check(base + "&hash=" + "0" * 64) == "malformed"


@pytest.mark.parametrize("bad", ["A" * 64, "g" * 64, "0" * 63, "0" * 65])
def test_hash_must_be_64_lowercase_hex(bad):
    good = init()
    real = dict(p.split("=", 1) for p in good.split("&"))["hash"]
    assert check(good.replace(real, bad)) == "malformed"
    assert check(good.replace(real, real.upper())) == "malformed"


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), "ok"),
        (timedelta(minutes=15), "ok"),  # the limit itself
        (timedelta(minutes=15, seconds=1), "stale"),
        (timedelta(hours=2), "stale"),
        (-timedelta(seconds=60), "ok"),  # ahead, within the allowed skew
        (-timedelta(seconds=61), "future"),
        (-timedelta(days=1), "future"),
    ],
)
def test_auth_date_window(age, expected):
    assert check(init(age=age)) == expected


@pytest.mark.parametrize("stamp", ["abc", "-5", "1.5", "", "9" * 13, " 1"])
def test_auth_date_must_be_plain_digits(stamp):
    fields = {"auth_date": stamp, "user": json.dumps({"id": OWNER})}
    fields["hash"] = hash_of(fields)
    assert check(urlencode(fields)) == "malformed"


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (json.dumps({"id": OWNER}), "ok"),
        (json.dumps({"id": OWNER + 1}), "forbidden"),
        (json.dumps({"id": str(OWNER)}), "malformed"),  # a string is not an id
        (json.dumps({"id": float(OWNER)}), "malformed"),
        (json.dumps({"id": True}), "malformed"),
        (json.dumps({"id": None}), "malformed"),
        (json.dumps({"first_name": "no id"}), "malformed"),
        (json.dumps([OWNER]), "malformed"),
        (json.dumps("text"), "malformed"),
        ("{not json", "malformed"),
        ("", "malformed"),
    ],
)
def test_user_must_be_the_owner_with_an_integer_id(user, expected):
    assert check(init(user=user)) == expected


def test_a_signed_message_for_another_account_is_forbidden_not_malformed():
    assert check(init(user=json.dumps({"id": 7}))) == "forbidden"


def test_oversized_input_is_refused_before_parsing():
    huge = init(pad="x" * (MAX_BODY_BYTES + 1))
    assert check(huge) == "malformed"


def test_too_many_fields_are_refused():
    extras = {f"k{i}": "v" for i in range(40)}
    assert check(init(**extras)) == "malformed"


def test_error_codes_never_carry_input():
    secret = "SECRET-VALUE-9f3"
    for raw in (init(pad=secret, token="1:other"), "x=" + secret):
        with pytest.raises(InitDataError) as caught:
            validate_init_data(raw, TOKEN, OWNER, NOW)
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
