"""Placeholder references in docs, and many secret-named objects in one text."""

from __future__ import annotations

import time

import pytest

from kiro_crew.deploy.scan import scan_content
from kiro_crew.security.redaction import redact_credentials


@pytest.mark.parametrize(
    "doc",
    [
        "api_key: <your-api-key>\n",
        "password: ********\n",
    ],
)
def test_a_reference_in_place_of_a_secret_is_an_overridable_placeholder(doc: str) -> None:
    findings = [f for f in scan_content(doc) if f.kind.startswith("credential")]
    assert findings, doc
    assert all(
        f.kind == "credential-placeholder" and f.severity == "info" for f in findings
    ), findings


@pytest.mark.parametrize(
    "doc", ["api_key: Zq9vT2mXw8KpL4rB\n", "<api_key><![CDATA[Zq9vT2mXw8KpL4rB]]></api_key>\n"]
)
def test_a_real_value_behind_the_same_label_stays_a_credential(doc: str) -> None:
    findings = scan_content(doc)
    assert any(f.severity == "credential" for f in findings), findings


@pytest.mark.parametrize(
    "doc",
    [
        "Set api_key: <your-api-key> in your config.\n",
        "Put <your-api-key> in the header.\n",
        "see <db-password> for details\n",
    ],
)
def test_an_angle_bracket_name_in_prose_holds_no_value(doc: str) -> None:
    assert redact_credentials(doc)[0] == doc
    assert not [f for f in scan_content(doc) if f.severity == "credential"]


@pytest.mark.parametrize(
    "doc",
    [
        "<password> s3cr3tVal9 </password>\n",
        "<password>s3cr3tVal9</password>\n",
        "<password>\n  s3cr3tVal9\n</password>\n",
    ],
)
def test_an_xml_element_value_is_still_redacted(doc: str) -> None:
    assert "s3cr3tVal9" not in redact_credentials(doc)[0]


def _entries(count: int) -> str:
    return "[" + ",".join('{"name":"x_token","value":"v%d"}' % i for i in range(count)) + "]"


def test_many_secret_named_objects_are_read_in_linear_time() -> None:
    small, large = _entries(1000), _entries(8000)
    redact_credentials(small)
    started = time.perf_counter()
    redact_credentials(small)
    small_s = time.perf_counter() - started
    started = time.perf_counter()
    out = redact_credentials(large)[0]
    large_s = time.perf_counter() - started
    assert '"value":"v7999"' not in out
    # Eight times the text: a per-field rescan of the object reach makes this
    # far more than eight times as slow, and seconds long.
    assert large_s < 24 * small_s + 0.5, (small_s, large_s)


def test_each_object_keeps_its_own_fields() -> None:
    text = (
        '[{"name":"DB_PASSWORD","x":{"a":"}"},"value":"hunter2Zq9"},'
        '{"value":"keepMe"},{"value":"Zq8hunter","name":"API_TOKEN"}]'
    )
    out = redact_credentials(text)[0]
    assert "hunter2Zq9" not in out and "Zq8hunter" not in out
    assert '"value":"keepMe"' in out


def _views(text: str, size: int) -> list[str]:
    from kiro_crew.log_redaction import SecretRedactionFilter
    from kiro_crew.security.text_redaction import StreamRedactor

    redactor = StreamRedactor()
    streamed = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return [
        redact_credentials(text)[0],
        streamed + redactor.flush(),
        SecretRedactionFilter([]).redact(text),
    ]


@pytest.mark.parametrize("delim", ["%2C", "%3B", "%2c", "%3b"])
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_an_encoded_password_with_a_blank_and_a_delimiter_is_redacted_whole(
    delim: str, size: int
) -> None:
    text = f"https://x/cb?next=pass%77ord%3DQx7ab%20cd{delim}efKp9Z%26u%3D1 done"
    # The log filter reads no encoded views (an encoded label is not one there).
    for out in _views(text, size)[:2]:
        assert "Qx7ab" not in out and "efKp9Z" not in out, out
        assert out.endswith("%26u%3D1 done"), out
    assert [f for f in scan_content(text) if f.severity == "credential"]


_TWILIO_SID = "SK" + "0123456789abcdef" * 2


@pytest.mark.parametrize(
    "text",
    [
        '{"name":"TWILIO_API_KEY","value":"%s"}' % _TWILIO_SID,
        '{"value":"%s","name":"TWILIO_API_KEY"}' % _TWILIO_SID,
        '[{"Name":"TWILIO_API_KEY","Value":"%s"}]' % _TWILIO_SID,
        '{"name":"TWILIO_API_KEY","type":"PLAINTEXT","value":"%s"}' % _TWILIO_SID,
    ],
)
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_twilio_key_sid_in_a_sibling_value_keeps_its_identifier_tier(
    text: str, size: int
) -> None:
    for out in _views(text, size):
        assert _TWILIO_SID in out, out
    assert not [f for f in scan_content(text) if f.severity == "credential"]


def test_a_sibling_secret_beside_a_twilio_name_is_still_redacted() -> None:
    text = '{"name":"TWILIO_AUTH_TOKEN","value":"Zq9vT2mXw8KpL4rBzz"}'
    assert "Zq9vT2mXw8" not in redact_credentials(text)[0]


@pytest.mark.parametrize(
    "text",
    [
        "first_pass: completed\n",
        "pwd: /usr/local\n",
        "refresh token: run kinit\n",
        "second_pass: done\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_status_or_path_behind_a_weak_label_stays(text: str, size: int) -> None:
    for out in _views(text, size):
        assert out == text, out
    assert not [f for f in scan_content(text) if f.severity == "credential"]


@pytest.mark.parametrize(
    "text,secret",
    [
        ("first_pass: Zq9vT2mXw8\n", "Zq9vT2mXw8"),
        ("DB_PASS=hunter\n", "hunter"),
        ("refresh token: Zq9vT2mXw8\n", "Zq9vT2mXw8"),
        ("password: run kinit\n", "run"),
    ],
)
def test_a_secret_behind_the_same_labels_is_still_redacted(text: str, secret: str) -> None:
    for out in _views(text, 7):
        assert secret not in out, out


@pytest.mark.parametrize("size", [1, 7, 4096])
def test_a_url_password_ends_at_the_host_and_a_query_value_at_the_fragment(size: int) -> None:
    token = "ghp_" + "a" * 36
    clone = f"git clone https://x-access-token:{token}@github.com/org/repo.git done"
    query = "https://host/cb?access_token=abcd1234efgh5678#frag"
    # The log filter masks a URL's user info its own way (``x-access-token:[REDACTED]``).
    for out in _views(clone, size)[:2]:
        assert token not in out and out.endswith("github.com/org/repo.git done"), out
    for out in _views(query, size)[:2]:
        assert "abcd1234" not in out and out.endswith("#frag"), out


@pytest.mark.parametrize("size", [1, 7, 64])
@pytest.mark.parametrize(
    "label", ["\x1b[33mpassword\x1b[0m", "\x1b[1;33mdb_password\x1b[0m", "pass\\u0077ord"]
)
def test_a_block_scalar_behind_a_coloured_label_streams_redacted(label: str, size: int) -> None:
    from kiro_crew.security.text_redaction import StreamRedactor

    text = f"{label}: |\n  Zq9vT2mXw8KpL4rB\nnext: ok\n" + "diagnostic line\n" * 40
    redactor = StreamRedactor()
    chunks = [redactor.feed(text[i : i + size]) for i in range(0, len(text), size)]
    streamed = "".join(chunks)
    assert "Zq9vT2mXw8" not in streamed, streamed
    assert "Zq9vT2mXw8" not in streamed + redactor.flush()
    assert "Zq9vT2mXw8" not in redact_credentials(text)[0]


@pytest.mark.parametrize("label", ["db_password", "DB_PASSWORD", "app.passwd", "gpg-passphrase"])
def test_a_glued_pass_gives_way_to_the_whole_label_word(label: str) -> None:
    from kiro_crew.security import redaction_labels

    text = f"{label}: "
    assert redaction_labels.stream_hold_start(text) == text.lower().index("pass")
    assert redaction_labels.stream_hold_start("db_pass: ") == 2


@pytest.mark.parametrize(
    "text",
    [
        "const c = {\n  user: u,\n  password: input.value,\n};\n",
        "const c = {\n  password: form.pw.value\n};\n",
        "fetch(u, {\n  headers: {\n    apiKey: cfg.apiKey,\n  },\n});\n",
        'login({\n  note: "a } b",\n  password: getPassword(),\n});\n',
    ],
)
def test_an_object_property_expression_in_code_is_no_config_literal(text: str) -> None:
    assert redact_credentials(text)[0] == text
    assert not [f for f in scan_content(text) if f.severity == "credential"]


@pytest.mark.parametrize(
    "text,secret",
    [
        ("const c = {\n  password: 'Zq9vT2mXw8',\n};\n", "Zq9vT2mXw8"),
        ("const c = {\n  password: Zq9vT2mXw8\n};\n", "Zq9vT2mXw8"),
        ("db:\n  password: Zq9vT2mXw8\n", "Zq9vT2mXw8"),
        ("{{- if .Values.db }}\ndb:\n  password: abc.def\n{{- end }}\n", "abc.def"),
        ("f() {\n  PASSWORD=Zq9vT2mXw8\n}\n", "Zq9vT2mXw8"),
    ],
)
def test_a_literal_beside_or_outside_an_object_is_still_redacted(text: str, secret: str) -> None:
    assert secret not in redact_credentials(text)[0]
    assert [f for f in scan_content(text) if f.severity == "credential"]


def test_config_lines_after_closed_braces_are_still_config_lines() -> None:
    text = "{}" * 2000 + "\n" + "".join(f"  db_password: Zq9vT2mX{i:05d}\n" for i in range(3000))
    out = redact_credentials(text)[0]
    assert "Zq9vT2mX" not in out


@pytest.mark.parametrize("pad", [" ", " " * 9, " " * 40, "\t" * 12, "\n" + " " * 10])
@pytest.mark.parametrize(
    "template",
    [
        '{{"value":{pad}"Zq9vT2mXw8KpL4rBzz", "name":{pad}"SecretAccessKey"}}',
        '{{"name"{pad}:{pad}"DB_PASSWORD", "type": "P", "value"{pad}:{pad}"Zq9vT2mXw8KpL4rBzz"}}',
    ],
)
@pytest.mark.parametrize("size", [1, 7, 4096])
def test_padded_sibling_fields_are_read_with_the_shared_separator(
    template: str, pad: str, size: int
) -> None:
    text = template.format(pad=pad)
    # The log filter reads no sibling name/value pairs, on main as here.
    for out in _views(text, size)[:2]:
        assert "Zq9vT2mXw8" not in out, out
    assert [f for f in scan_content(text) if f.severity == "credential"]


# ── Round on 34650714cb (GPT, Opus): escaped objects and blocks, holders ──


def _streamed(text: str, size: int) -> str:
    from kiro_crew.security.text_redaction import StreamRedactor

    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


_ESC_PW = "Zq9vT2mXw8Lp"


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(
    "order", [("value", "description", "name"), ("value", "name"), ("name", "description", "value")]
)
@pytest.mark.parametrize("size", [1, 2, 7])
def test_an_escaped_value_first_object_holds_the_value_for_its_name(
    depth: int, order: tuple[str, ...], size: int
) -> None:
    import json

    fields = {"value": _ESC_PW, "description": "", "name": "DB_PASSWORD"}
    text = json.dumps({key: fields[key] for key in order})
    for _ in range(depth):
        text = json.dumps(text)[1:-1]
    assert _ESC_PW not in _streamed(text, size)
    assert _ESC_PW not in redact_credentials(text)[0]


@pytest.mark.parametrize("sep", [";", ",", "&"])
@pytest.mark.parametrize("size", [1, 7])
def test_an_escaped_yaml_block_keeps_its_whole_value_when_it_closes(sep: str, size: int) -> None:
    import json

    suffix = "Tz8YwXv6Uq"
    body = "password: |\n  Ab3dEfGh5jKl" + sep + suffix + "\nnext: ok\n"
    text = '{"x": ' + json.dumps(body) + "}"
    out = _streamed(text, size)
    assert suffix not in out and "Ab3dEf" not in out
    assert out.endswith('next: ok\\n"}')


_UUID = "8f14e45f-ceea-167a-5a36-dedd4bea2543"


@pytest.mark.parametrize(
    "text",
    [
        "client_id=x client_secret=" + _UUID,
        "client_id=x&client_secret=" + _UUID,
        "client id: abc, password: " + _UUID,
    ],
)
def test_an_identifier_label_earlier_on_the_line_never_vouches_for_a_secret(text: str) -> None:
    for out in (redact_credentials(text)[0], _streamed(text, 7)):
        assert _UUID not in out, out


def test_an_azure_client_id_behind_its_own_label_keeps_its_tier() -> None:
    text = "AZURE_CLIENT_ID=" + _UUID + "\n"
    assert redact_credentials(text)[0] == text


@pytest.mark.parametrize(
    "text",
    [
        "url = `${base}?sig=${sig}`",
        "https://acct.blob.core.windows.net/c?sv=1&sig=YOUR_SAS_SIGNATURE",
        "see ?sig=[REDACTED] here",
    ],
)
def test_a_signing_parameter_written_without_a_value_is_no_hard_finding(text: str) -> None:
    assert [f for f in scan_content(text) if f.severity == "credential"] == []


def test_a_real_signing_value_is_still_a_hard_finding() -> None:
    text = "https://a/b?sp=r&sig=" + "Ab3dEfGh5jKlMn7pQr9s" + "Tz8YwXv6UqLm%3D"
    assert [f for f in scan_content(text) if f.severity == "credential"]
