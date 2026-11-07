"""Nullable tool fields use explicit alternatives accepted by the live API.

The deployed API rejected the otherwise-valid schema using type arrays for nullable
arrays/scalars. Explicit anyOf alternatives preserve the same validation semantics.
"""

from xboct.llm.contract import IntentEnvelope
from xboct.llm.schema import check_strict_subset, strict_schema


def test_nullable_tool_fields_use_explicit_alternatives():
    schema = strict_schema(IntentEnvelope)
    assert check_strict_subset(schema) == []
    actions = schema["properties"]["actions"]["anyOf"]
    assert actions[-1] == {"type": "null"}
    assert actions[0]["type"] == "array"
    assert actions[0]["maxItems"] == 20
    confirmation = schema["properties"]["requires_confirmation"]["anyOf"]
    assert confirmation == [{"type": "boolean"}, {"type": "null"}]

    def walk(value):
        if isinstance(value, dict):
            assert not isinstance(value.get("type"), list)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
