import pytest

from xboct.domain.money import minor_to_input, parse_major


@pytest.mark.parametrize(
    ("minor", "text"),
    [
        (0, "0"),
        (1_000, "10"),
        (10_000, "100"),
        (100_000, "1000"),
        (120_000, "1200"),
        (120_050, "1200,5"),
        (120_055, "1200,55"),
        (7, "0,07"),
        (4_500_000, "45000"),
    ],
)
def test_form_text_reads_back_to_the_same_minor_units(minor, text):
    assert minor_to_input(minor, "RUB") == text
    assert parse_major(text, "RUB") == minor
