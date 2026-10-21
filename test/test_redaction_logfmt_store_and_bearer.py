"""A later logfmt field, web URLs behind a secret label, bare Bearer tokens in
logs, and the cost of many name/value fields with no braces around them."""

from __future__ import annotations

import json

import pytest

from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security import redaction_labels
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.text_redaction import StreamRedactor

_PASSPHRASE = "correct" + "_horse_battery_staple"
_HOOK = "https://hooks.zapier.com/hooks/catch/" + "123456/abcdefQ/"


def _stream(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


def _views(text: str) -> list[str]:
    return [
        redact_credentials(text)[0],
        _stream(text, 1),
        _stream(text, 7),
        SecretRedactionFilter([]).redact(text),
    ]


@pytest.mark.parametrize(
    "line",
    [
        f"level=info password={_PASSPHRASE} user=bob",
        f'ts=1 msg="hi there" api_key={_PASSPHRASE} id=7',
    ],
)
def test_a_later_logfmt_field_is_a_literal(line: str) -> None:
    for out in _views(line):
        assert _PASSPHRASE not in out, out
        # The next field stays: a logfmt value ends at the blank before it.
        assert out.rstrip().endswith(("user=bob", "id=7")), out


@pytest.mark.parametrize(
    "line",
    ["password = new_password", "f(a=1, password=new_password)", "    a=1, password=new_password)"],
)
def test_code_with_assignments_before_the_label_is_kept(line: str) -> None:
    for out in _views(line):
        assert out == line


def test_a_web_url_behind_a_secret_label_is_a_secret() -> None:
    text = '{"webhook_secret": "' + _HOOK + '"}'
    for out in _views(text):
        assert _HOOK not in out, out


@pytest.mark.parametrize("value", ["op://vault/item/password", "vault://kv/app/db"])
def test_a_secret_store_reference_is_kept(value: str) -> None:
    text = '{"db_password": "' + value + '"}'
    assert redact_credentials(text)[0] == text


@pytest.mark.parametrize("token", ["abcdefghijkl", "authenticationxyz12", "ABCDEFGHIJ"])
def test_a_single_case_bearer_token_is_redacted_in_logs(token: str) -> None:
    out = SecretRedactionFilter([]).redact(f"stderr: Bearer {token} sent")
    assert token not in out, out


def test_prose_after_bearer_is_kept_in_logs() -> None:
    line = "Bearer authentication failed"
    assert SecretRedactionFilter([]).redact(line) == line


def test_each_value_field_is_judged_against_at_most_two_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Many name/value fields with no braces: each value pairs with its
    # nearest names, not with every name in the object reach.
    line = '"name": "password", "value": "Xq7m2pz9Rtv"\n'
    text = line * 200
    calls = 0
    judge = redaction_labels._value_is_secret

    def counted(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        return judge(*args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as mp:
        mp.setattr(redaction_labels, "_value_is_secret", counted)
        out = redaction_labels._object_name_value_spans(text)
    assert len(out) == 200
    assert calls <= 2 * 200, calls


_CONSOLE_PW = "Xq7m2pz9" + "Rtv4Lk"


@pytest.mark.parametrize(
    "entry",
    [f'password="{_CONSOLE_PW}"', f'password=")bc{_CONSOLE_PW}"'],
)
def test_an_escaped_quoted_assignment_inside_a_json_string_is_redacted(entry: str) -> None:
    # A console entry serialised as JSON: the quote before the value is an
    # escaped one, so it opens the value rather than closing a caption.
    text = json.dumps({"text": entry})
    for out in [redact_credentials(text)[0], _stream(text, 1), _stream(text, 7)]:
        assert _CONSOLE_PW not in out, out


@pytest.mark.parametrize(
    "line", ['getpass("Password:")', 'print("Token:", t)', 'unknown key "api_key:" in config']
)
def test_a_caption_literal_stays_when_serialised_as_json(line: str) -> None:
    text = json.dumps({"m": line})
    assert redact_credentials(text)[0] == text


def test_a_vendor_key_spelling_a_placeholder_word_is_still_redacted() -> None:
    # A generated 1Password secret key whose random segment spells FALSE: only a
    # value that IS true/false/null is a placeholder.
    key = "A3-" + "ABC123-" + "FALSEXY9Q2W" + "-K7M2P-Z9R4T-V8N3L"
    out = redact_credentials(f"key {key}")[0]
    assert key not in out, out
