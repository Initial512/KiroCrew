"""Redaction of config-line literals, plain words behind strong labels, PEM
bodies in the stream, keys inside URLs, and text inside binary decodes.

Split from ``test_redaction_layer_and_stream_reach`` at the file line cap."""

from __future__ import annotations

import json
import random
import re
import string

import pytest

from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.text_redaction import StreamRedactor


def _redact(text: str) -> str:
    return redact_credentials(text)[0]


def _stream(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


# ── F1/F3 on 457b4ddc96: plain words behind strong labels, config-line literals ──


@pytest.mark.parametrize(
    "text",
    [
        "x password: hunter",  # a short plain word
        "x password: secretpass",  # a lower-case word of 8-15 letters
        "x api_key: Abc1234",  # a short mixed value
        "x secret= ok",
        "check(password=letmein)",
    ],
)
def test_a_plain_word_behind_a_strong_label_is_redacted(text: str) -> None:
    value = re.split(r"[ =]", text.rstrip(")"))[-1]
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert value not in out


@pytest.mark.parametrize(
    "text",
    [
        "password = new_password",  # a snake_case identifier
        "token = get_token()",  # a call
        "login(password=load_key())",
        "x secret = settings.db_password",  # a dotted name
        "x token: os.environ['TOKEN']",  # an indexed read
        "x api_key = userApiKey",  # a camelCase identifier
        "x password=${DB_PASSWORD}",  # a variable reference
    ],
)
def test_visibly_code_behind_a_strong_label_is_kept(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text
    assert _stream(text, 1) == text


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("PASSWORD=hunter2\n", "hunter2"),
        ("password=letmein\n", "letmein"),
        ("DB_PASS=a,b;c\n", "a,b;c"),
        ("DB_PASSWORD=new_password\n", "new_password"),  # no word or code exemption
        ("password: secretpass\n", "secretpass"),
        ("secret: settings.db_secret\n", "settings.db_secret"),
        ("export API_KEY=a,b;cdefgh # rotate\n", "a,b;cdefgh"),
        ("  password: two words, one;semi\nnext: x\n", "two words, one;semi"),
        ("PASSWORD=pw,with;punct&next=1 tail\n", "pw,with;punct&next=1 tail"),
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_config_line_value_is_a_literal_read_to_its_line_end(
    text: str, value: str, size: int
) -> None:
    batch = _redact(text)
    assert _stream(text, size) == batch
    for out in (batch, SecretRedactionFilter([]).redact(text)):
        for piece in re.split(r"[,;& ]", value):
            if len(piece) > 1:
                assert piece not in out
    if "# rotate" in text:
        assert batch.endswith(" # rotate\n")
    if "next: x" in text:
        assert batch.endswith("\nnext: x\n")


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=${DB_PASSWORD}\n",
        "DB_PASSWORD=$PW\n",
        "api_key: ${API_KEY}\n",
        "SECRET_ARN=arn:aws:secretsmanager:us-east-1:123456789012:secret:x\n",
        "PWD=/workplace/project\n",  # the shell's working directory, not a password
    ],
)
def test_a_config_line_keeps_only_an_exact_reference_or_an_identifier(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text
    assert _stream(text, 1) == text


# ── Round on b1228c3ff9: PEM bodies in the stream, keys in URLs, binary decodes ──


def _pem(nbytes: int, kind: str = "RSA PRIVATE KEY") -> tuple[str, list[str]]:
    import base64

    rng = random.Random(nbytes)
    body = base64.b64encode(bytes(rng.randrange(256) for _ in range(nbytes))).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {kind}-----\n" + "\n".join(lines) + f"\n-----END {kind}-----", lines


@pytest.mark.parametrize("nbytes", [1190, 4800, 9000])  # 2048-bit, larger, past the reach
@pytest.mark.parametrize("tail", ["\ndone\n", ""])
@pytest.mark.parametrize("size", [1, 7, 64, 500])
def test_a_pem_private_key_never_streams_its_body(nbytes: int, tail: str, size: int) -> None:
    pem, lines = _pem(nbytes)
    text = "Here is the key:\n" + pem + tail
    out = _stream(text, size)
    assert not [line for line in lines if line in out]
    assert out == _redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/download?k=wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzAMPLEKEY",
        "https://x.com/wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzAMPLEKEY end",
    ],
)
def test_the_bundle_gate_refuses_a_key_with_slashes_inside_a_url(text: str) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert "bare-secret" in [leak.kind for leak in scan.scan_text(text, "skill")]


def test_a_binary_decode_is_read_for_fixed_patterns_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    from kiro_crew.security import redaction as facade

    rng = random.Random(2)
    blob = base64.b64encode(bytes(rng.randrange(256) for _ in range(30_000))).decode()

    real_views = facade.normalised_views

    def _no_views(text: str) -> list[object]:
        # Only the decode's short text runs may be read, never the binary whole.
        assert len(text) < 1024, "a binary decode built its normalised views"
        return real_views(text)

    with monkeypatch.context() as mp:
        mp.setattr(facade, "normalised_views", _no_views)
        assert _redact(f"logo {blob} end") == f"logo {blob} end"
    # Text decodes are still read through their views and context rules.
    secret = base64.b64encode(b'{"password": "Xq7m2pzRtvB8nc1k"}').decode()
    assert secret not in _redact(f"cfg {secret} end")


# ── Round on 6e2be1afa7: non-ASCII decoded text, a config value equal to its label ──


@pytest.mark.parametrize("password", ["пароль12", "密码密码密码12", "contraseña1"])
def test_an_encoded_non_ascii_password_is_text_not_binary(password: str) -> None:
    import base64

    blob = base64.b64encode(json.dumps({"password": password}, ensure_ascii=False).encode())
    text = f"payload {blob.decode()} end"
    for out in (_redact(text), _stream(text, 7)):
        assert blob.decode() not in out


@pytest.mark.parametrize("text", ["password: password\n", "  password: Password\n"])
@pytest.mark.parametrize("size", [1, 64])
def test_a_config_value_equal_to_its_label_is_redacted(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert out.rstrip().endswith("]")


def test_a_caption_in_markup_still_stays() -> None:
    text = "<label>Password:</label>\n"
    assert _redact(text) == text == _stream(text, 1)


# ── Round on af65c03a3e: literal data values, text inside binary, HTML separators ──


@pytest.mark.parametrize(
    "text",
    [
        "<config><password>correct_horse_battery</password></config>\n",
        "- name: DB_PASSWORD\n  value: correct_horse_battery\n",
        '{"name": "API_SECRET", "value": "settings.key"}\n',
    ],
)
def test_xml_content_and_sibling_values_are_literals(text: str) -> None:
    value = "correct_horse_battery" if "correct" in text else "settings.key"
    for out in (_redact(text), _stream(text, 1)):
        assert value not in out
    if text.startswith("<config>"):
        assert value not in SecretRedactionFilter([]).redact(text)


def test_a_reference_in_xml_content_is_still_kept() -> None:
    text = "<password>${DB_PW}</password>\n"
    assert _redact(text) == text


def _png(text_chunk: bytes) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rng = random.Random(1)
    noise = bytes(rng.randrange(256) for _ in range(5000))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", b"\x00" * 13)
        + chunk(b"tEXt", text_chunk)
        + chunk(b"IDAT", noise)
    )


def test_a_labelled_secret_in_a_binary_decodes_text_chunk_is_found() -> None:
    import base64

    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"
    body = base64.b64encode(_png(b"Comment\x00aws_secret_access_key=" + secret.encode())).decode()
    assert body not in _redact(f'<img src="data:image/png;base64,{body}">')


def test_utf16_encoded_text_is_read_as_text() -> None:
    import base64

    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"
    command = f"$env:AWS_SECRET_ACCESS_KEY='{secret}'; aws s3 ls"
    blob = base64.b64encode(command.encode("utf-16-le")).decode()
    assert blob not in _redact(f"powershell -EncodedCommand {blob}")


def test_an_ordinary_bitmap_body_is_not_condemned_by_chance_vendor_shapes() -> None:
    import base64

    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    # A body that happens to hold a vendor-rule shape after a ``+`` or ``/``.
    head = base64.b64encode(_png(b"Comment\x00plain")[:48]).decode()
    body = head + "Yk+BSAnADr2kmhQ7Lp3VxZcR8wTbN4yF" + "A" * 64
    assert routes._scanned_bitmap_bytes(body)[1] is False


@pytest.mark.parametrize(
    "link",
    [
        "https://a.blob.core.windows.net/c?sv=1&amp;s%69g=SECRETSIGVALUE123&amp;se=2",
        "https://a/c?sv=1&#38;sig=SECRETSIGVALUE123",
        "https://a/c?sv=1&#x26;sig=SECRETSIGVALUE123",
    ],
)
def test_an_html_escaped_query_separator_still_reads_sig(link: str) -> None:
    assert "SECRETSIGVALUE123" not in SecretRedactionFilter([]).redact(link)


# ── Round on 14f0ff400e: vendor context for views and decodes, null literals, slash runs ──


@pytest.mark.parametrize("encode", ["percent", "plain"])
def test_a_vendor_keyword_far_before_an_encoded_token_still_selects_the_rule(encode: str) -> None:
    from urllib.parse import quote

    rng = random.Random(9)
    token = (
        "pat"
        + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(14))
        + "."
        + "".join(rng.choice("0123456789abcdef") for _ in range(64))
    )
    body = quote(f'"{token}"') if encode == "percent" else token
    text = "# Airtable\n" + "Lorem ipsum dolor sit amet. " * 300 + "\n" + body + "\n"
    assert token[3:20] not in _redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "<script>const password = null;</script>",
        '{"password": null, "token": false}',
        "x password = None",
    ],
)
def test_a_null_or_boolean_literal_behind_a_label_is_kept(text: str) -> None:
    assert _redact(text) == text


@pytest.mark.parametrize("text", ["PASSWORD=false\n", "PASSWORD=null\n", "x password = nullish"])
def test_a_config_literal_or_a_word_like_null_is_still_redacted(text: str) -> None:
    assert _redact(text) != text


def test_a_url_run_of_only_slashes_does_not_crash_the_bundle_gate() -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    assert scan.scan_text("see https://x.test/?q=" + "/" * 45 + " ok", "prompt") == []


# ── Round on be09090e68: YAML doubled quotes, URL query values ──


@pytest.mark.parametrize(
    "text",
    [
        "password: 'it''s-Secret9xyz'\nnext: 1\n",
        "db:\n  password: 'a''b''cdefgh9'\n",
        "x password: 'it''s" + "Q7w" * 1700 + "''tail9Secret' and more\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_yaml_doubled_quote_does_not_end_a_password(text: str, size: int) -> None:
    pieces = ("Secret9xyz", "cdefgh9", "tail9Secret", "Q7wQ7wQ7w")
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert not [p for p in pieces if p in out]


def test_an_empty_single_quoted_value_still_closes_at_once() -> None:
    text = "x password='' and y=1"
    assert _redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "GET /login?user=a&password=correct_horse_battery&x=1",
        "https://h/p?password=correct_horse_battery",
        "https://h/p?a=1&amp;api_key=userApiKey",
    ],
)
def test_a_url_query_value_is_a_literal(text: str) -> None:
    value = "correct_horse_battery" if "horse" in text else "userApiKey"
    for out in (_redact(text), _stream(text, 3), SecretRedactionFilter([]).redact(text)):
        assert value not in out
    if "&x=1" in text:
        assert _redact(text).endswith("&x=1")


# ── Round on 67f70511c7: a config value that starts with a delimiter ──


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=;Xq7m2pz9Rtv\n",
        "password=[Xq7m2pz9Rtv\n",
        "API_KEY=\\Xq7m2pz9Rtv\n",
        "password: {abc}Xq7m2pz9Rtv\n",
        "db_password: ;Xq7m2pz9Rtv # note\n",
        "export API_TOKEN=&Xq7m2pz9Rtv\n",
        "- secret: (Xq7m2pz9Rtv\n",
        "PASSWORD=<hidden Xq7m2pz9Rtv>\n",  # not the digest mask's exact shape
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_config_value_led_by_a_delimiter_is_redacted(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m2pz9" not in out
    if "# note" in text:
        assert _redact(text).endswith(" # note\n")


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD=[REDACTED: credential]\n",  # already a tag: never matched again
        "API_TOKEN=<hidden sha256:0123abcd>\n",  # the MCP display's digest mask
        "f(password, token)\n",
        "x password = get_token()\n",
    ],
)
def test_a_tag_or_code_after_a_label_is_unchanged(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text


# ── Round on 5a79d39671: escaped block scalars, operand keywords, inline media ──


@pytest.mark.parametrize(
    "yaml_text",
    [
        "db:\n  password: |\n    Xq7m2pz9Rtv\n    Zz8Kk3Lm\n  user: x\n",
        "password: |\r\n  Xq7m2pz9Rtv\r\n",
        "api_key: >-\r\n  Xq7m2pz9Rtv\r\nnext: 1\r\n",
    ],
)
@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_block_scalar_in_an_escaped_string_never_streams(
    yaml_text: str, layers: int, size: int
) -> None:
    text = yaml_text
    for _ in range(layers):
        text = json.dumps({"content": text})
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m2pz9" not in out and "Zz8Kk3" not in out
    if "next: 1" in yaml_text:
        assert "next: 1" in _redact(text)


def test_a_long_escaped_block_scalar_drops_its_body_past_the_hold() -> None:
    body = "".join("    Q7w%04dXyZ\n" % i for i in range(800))
    text = json.dumps({"content": "db:\n  password: |\n" + body + "  user: x\n"}) + " tail"
    for size in (7, 5000):
        out = _stream(text, size)
        assert "Q7w" not in out and out.endswith(" tail")


@pytest.mark.parametrize(
    "text",
    [
        "async function load() { const apiKey = await response.text(); }",
        "String password = new String(chars);",
        "x secret = typeof value",
    ],
)
def test_a_keyword_that_takes_an_operand_is_code(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text


@pytest.mark.parametrize("text", ["x password: await", "x password: new\n", "PASSWORD=await x\n"])
def test_an_operand_keyword_with_no_operand_or_on_a_config_line_is_redacted(text: str) -> None:
    assert _redact(text) != text


def test_an_inline_image_is_no_bare_secret_but_random_bytes_under_its_header_are() -> None:
    import base64

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    noise = random.Random(7).randbytes(600)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + noise).decode()
    assert scan.scan_text(f"![card](data:image/png;base64,{png})", "prompt") == []
    fake = base64.b64encode(noise[:30]).decode()
    assert scan.scan_text(f"see data:image/png;base64,{fake} end", "prompt") != []


@pytest.mark.parametrize(
    "text",
    [
        "if password > threshold:",  # a comparison, not a separator
        "secret:\n    secretName: my-tls\n",  # a mapping opens; no value is carried over
        "password:\n  user: x\n",
    ],
)
def test_a_comparison_or_a_mapping_after_a_label_is_no_value(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text


@pytest.mark.parametrize("text", ["<password>Xq7m2pz9Rtv</password>", "password:\n  Xq7m2pz9Rtv\n"])
def test_an_xml_element_or_a_next_line_value_is_still_redacted(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m2pz9" not in out


@pytest.mark.parametrize("size", [1, 16, 64])
def test_compact_json_with_a_generic_label_streams_whole(size: int) -> None:
    text = '{"id":123,"name":"alice","secret":false},' * 120
    assert _stream(text, size) == _redact(text) == text


def test_a_run_of_glued_labels_keeps_the_stream_buffer_bounded() -> None:
    text = "password=" * 4000
    redactor = StreamRedactor()
    peak = 0
    for i in range(0, len(text), 20):
        redactor.feed(text[i : i + 20])
        peak = max(peak, len(redactor._buf))
    redactor.flush()
    assert peak <= 4096 + 20


@pytest.mark.parametrize(
    "text",
    [
        "SECRET_ACCESS_KEY=\\\nwJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY\nNEXT=1\n",
        "DB_PASSWORD=ab\\\ncdXq7m2pz9\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7])
def test_a_continued_config_value_is_read_onto_its_next_line(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "K7MDENG" not in out and "cdXq7m" not in out
    if "NEXT=1" in text:
        assert _redact(text).endswith("\nNEXT=1\n")


def test_an_escaped_backslash_at_a_line_end_does_not_continue_it() -> None:
    assert _redact("PASSWORD=abc\\\\\nnext\n").endswith("\nnext\n")


# ── Round on 132a1f1662: type names, minified names, logfmt ──


@pytest.mark.parametrize(
    "text",
    [
        "def login(password: str, token: str)",
        "interface Creds { password: string }",
        "x password: str = None",
        "def f(password: Optional[str] = None)",
        "x password: SecretStr\n",
        "x token: string | undefined;",
        "const o={password:t,username:n};",
    ],
)
def test_a_type_name_or_a_minified_name_behind_a_label_is_code(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, 1), _stream(text, 7)):
        assert out == text
    assert SecretRedactionFilter([]).redact(text) == text
    assert [f for f in scan_content(text) if f.severity == "credential"] == []


@pytest.mark.parametrize(
    "text",
    [
        '{"password":"Password"}',  # a quoted literal stays a literal
        "x password: strong",  # a word that only starts like a type
        "x password: string2",
        "x password: ab",  # two characters is no minified name
        "PASSWORD=t\n",  # a config line's value is a literal
        "GET /x?token=s",  # a URL query value is a literal
    ],
)
def test_a_literal_or_a_near_miss_of_a_type_is_still_redacted(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    assert _redact(text) != text
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_logfmt_value_ends_at_the_next_field(size: int) -> None:
    text = "password=abc user=bob ip=1.2.3.4\n"
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "abc" not in out and out.endswith(" user=bob ip=1.2.3.4\n")


@pytest.mark.parametrize("size", [1, 7])
def test_a_single_assignment_still_reads_to_its_line_end(size: int) -> None:
    text = "PASSWORD=a b c\nnext\n"
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert " b c" not in out and out.endswith("\nnext\n")


# ── Round on 132a1f1662 (GPT, Opus): INI sections, YAML nulls, digests, binary decodes ──

_INI = "[database]\nhost = db.local\n; note\npassword = correct_horse_battery\nport = 5432\n"


@pytest.mark.parametrize(
    "text",
    [
        _INI,
        "[db]\npassword = Xq7m!2pz;9Rtv,abc\n",
        "[db]\n" + "".join("k%d = v\n" % i for i in range(400)) + "password = correct_horse\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_padded_key_in_an_ini_section_is_a_literal(text: str, size: int) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "horse" not in out and "Xq7m" not in out and "9Rtv" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []
    if "port = 5432" in text:
        assert _redact(text).endswith("\nport = 5432\n")


@pytest.mark.parametrize(
    "text", ["password = new_password\n", "[x]\ndef f():\n    password = new_password\n"]
)
def test_a_padded_assignment_outside_an_ini_section_is_code(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text


@pytest.mark.parametrize("text", ["a: 1\npassword: null\nclientSecret: false\n", "token: ~\n"])
def test_a_yaml_null_or_boolean_is_no_secret(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert out == text
    assert [f for f in scan_content(text) if f.severity == "credential"] == []


def test_a_base64_sha_digest_is_no_bare_secret() -> None:
    import base64
    import hashlib

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    for algo in (hashlib.sha256, hashlib.sha512):
        digest = base64.b64encode(algo(b"asset-1").digest()).decode()
        assert scan.scan_text(f"see {digest} ok", "prompt") == []
    key = base64.b64encode(random.Random(3).randbytes(30)).decode()
    assert scan.scan_text(f"see {key} ok", "prompt") != []


def test_a_binary_decode_blocks_only_on_its_fixed_patterns_and_text_runs() -> None:
    import base64

    from kiro_crew.security import redaction_decode

    pair = b"AKIAIOSFODNN7EXAMPLE secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY"
    binary = random.Random(5).randbytes(4000)
    assert not redaction_decode._encoded_blocks(base64.b64encode(binary).decode())
    wrapped = base64.b64encode(binary[:200] + pair + binary[200:400]).decode()
    assert redaction_decode._encoded_blocks(wrapped)


# ── Round on 0bae467eb0 (GPT, Opus) ──


@pytest.mark.parametrize(
    "text",
    [
        'password = b"Xq7m2pz9Rtv"',
        'client_secret = r"Xq7m2pz9Rtv"',
        "api_key = rb'Xq7m2pz9Rtv'",
        'password = f"Xq7m2pz9Rtv"',
    ],
)
def test_a_python_prefixed_string_is_read_whole(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), _stream(text, 7)):
        assert "Xq7m" not in out
    assert "Xq7m" not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "value",
    ["eyJwYXNzd29yZCI6ICJhfn5-P2IifQ", "eyJwYXNzd29yZCI6Ing_Pj9-In0"],
)
def test_a_short_url_safe_base64_password_is_redacted(value: str) -> None:
    text = f"see {value} ok"
    for out in (_redact(text), _stream(text, 1), _stream(text, 7)):
        assert value not in out and out.endswith(" ok")


@pytest.mark.parametrize("label", ["password=", "aws_secret_access_key="])
def test_an_overflowing_config_value_drops_its_continued_lines(label: str) -> None:
    text = label + "Q" * 5100 + "\\\nXq7m2pz9Rtv\nnext\n"
    for size in (1, 512):
        out = _stream(text, size)
        assert "Xq7m" not in out and out.endswith("next\n")
    assert "Xq7m" not in _redact(text)


def test_a_deeply_indented_block_scalar_keeps_its_sibling_past_the_hold() -> None:
    body = "".join(" " * 46 + "Q7w%04dXyZ\n" % i for i in range(600))
    text = " " * 44 + "password: |\n" + body + " " * 44 + "user: bob\n"
    for size in (7, 64):
        out = _stream(text, size)
        assert "Q7w" not in out and "user: bob" in out


@pytest.mark.parametrize("pre", [395, 396, 397, 398, 700, 1001])
@pytest.mark.parametrize("size", [1, 3, 64])
def test_a_long_base64_run_never_streams_its_encoded_token(pre: int, size: int) -> None:
    import base64

    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
    blob = base64.b64encode(("x" * pre + " token " + token).encode()).decode()
    out = _stream(f"blob: {blob} done", size)
    assert blob[-48:] not in out and out.endswith(" done")


def test_a_run_of_xml_labels_is_read_once_each() -> None:
    text = "<password>" * 2000 + "<password>Xq7m2pz9Rtv</password>"
    assert "Xq7m" not in _redact(text)


@pytest.mark.parametrize("text", ["MYSQL_PWD=Xq7m2pz9Rtv\n", "x DB_PWD=Xq7m2pz9Rtv"])
def test_a_prefixed_pwd_is_a_password_in_the_log_too(text: str) -> None:
    assert "Xq7m" not in SecretRedactionFilter([]).redact(text)
    assert SecretRedactionFilter([]).redact("PWD=/srv/app\n") == "PWD=/srv/app\n"


def test_a_bare_secret_finding_shows_no_character_of_it() -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    leaks = scan.scan_text("see wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY ok", "prompt")
    assert leaks and all("wJal" not in leak.snippet for leak in leaks)


# ── Round on eb690bfc1f (GPT, Opus) ──

_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [f"s3cmd --secret-key {_KEY} ls", f"s3cmd --secret_key {_KEY}", f"--secret {_KEY} x"],
)
def test_a_secret_flag_before_a_key_of_its_shape_is_redacted(text: str) -> None:
    for out in (_redact(text), _stream(text, 1), _stream(text, 7)):
        assert _KEY[:12] not in out
    assert _KEY[:12] not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize("text", ["aws --secret-key notakey x", "the secret is out"])
def test_a_secret_flag_before_any_other_value_is_unchanged(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text


@pytest.mark.parametrize(
    "text",
    ["q=password%3Dab%20cd%22Xq7m2pz9Rtv&x=1", "password%3D%22ab%20cdXq7m2pz9Rtv%22"],
)
@pytest.mark.parametrize("size", [1, 7])
def test_an_encoded_value_with_an_encoded_blank_and_quote_is_read_whole(
    text: str, size: int
) -> None:
    for out in (_redact(text), _stream(text, size)):
        assert "Xq7m" not in out


@pytest.mark.parametrize("params", [20, 200])
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_long_terminal_escape_inside_a_token_never_splits_it(params: int, size: int) -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    text = "tok " + token[:9] + "\x1b[" + "1;" * params + "31m" + token[9:] + " end"
    out = _stream(text, size)
    assert token not in re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", out)
    assert out.endswith(" end")


# ── Round on f4cb752a48 (GPT): CDATA values, multi-line quoted YAML ──


@pytest.mark.parametrize(
    "text",
    [
        f"<SecretAccessKey><![CDATA[{_KEY}]]></SecretAccessKey>",
        "<password><![CDATA[Xq7m2pz9<Rtv]]></password> ok",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_cdata_value_behind_an_xml_label_is_read_whole(text: str, size: int) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert _KEY[:12] not in out and "Xq7m" not in out and "Rtv" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_quoted_yaml_value_over_indented_lines_is_read_whole(quote: str, size: int) -> None:
    text = f"db:\n  password: {quote}Xq7m2pz9\n    Rtv8Kk3Lm{quote}\n  user: x\n"
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m" not in out and "Rtv8" not in out and out.endswith("\n  user: x\n")


def test_a_quoted_value_ends_at_a_line_not_indented_deeper() -> None:
    text = 'db:\n  password: "Xq7m2pz9\n  user: x\n'
    assert _redact(text).endswith("\n  user: x\n")


# ── Round on 11fbc2d901 (GPT) ──


@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_blank_line_inside_a_quoted_yaml_value_does_not_end_it(size: int) -> None:
    text = 'db:\n  password: "Xq7m2pz9\n\n    Rtv8Kk3Lm"\n  user: x\n'
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m" not in out and "Rtv8" not in out and out.endswith("\n  user: x\n")


@pytest.mark.parametrize(
    "text",
    [
        "<password>correct_horse\nbattery_staple</password> ok",
        "<password>\n  correct_horse_battery\n</password>",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_an_xml_value_over_lines_is_a_literal(text: str, size: int) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "horse" not in out and "battery" not in out and "staple" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


@pytest.mark.parametrize(
    "text",
    [
        'password = """Xq7m2pz9Rtv"""\n',
        "password = '''Xq7m2pz9Rtv'''\n",
        'password = """Xq7m2pz9\nRtv8Kk3Lm"""\nnext = 1\n',
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_triple_quoted_password_is_read_to_its_triple_quote(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m" not in out and "Rtv8" not in out
    if "next = 1" in text:
        assert _redact(text).endswith("\nnext = 1\n")


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD%3Dcorrect_horse_battery",
        "HOST%3Dx%0APASSWORD%3Dcorrect_horse_battery%0APORT%3D1",
        "x\nPASSWORD%3Dcorrect_horse_battery\n",
    ],
)
def test_an_encoded_config_line_is_still_a_config_literal(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, 1), _stream(text, 7)):
        assert "horse" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


def test_an_encoded_label_mid_line_is_still_code() -> None:
    text = "see ?q=password%3Dnew_password ok"
    assert _redact(text) == text


# ── Round on 9adca1be3d (GPT) ──


@pytest.mark.parametrize("text", ["password:\n  AbCd1234:efGh5678\n", "password:\n  AbCd=1234\n"])
def test_a_next_line_value_with_a_colon_or_equals_is_still_the_value(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, 1), SecretRedactionFilter([]).redact(text)):
        assert "AbCd" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


@pytest.mark.parametrize(
    "text",
    [
        "password: AbCd1234\n  EfGh5678\nnext: ok\n",
        "db:\n  password: AbCd1234\n    EfGh5678\n\n    IjKl9012\n  user: x\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_folded_plain_yaml_value_is_read_through_its_lines(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert not [w for w in ("AbCd", "EfGh", "IjKl") if w in out]
        assert out.endswith(("next: ok\n", "  user: x\n"))


@pytest.mark.parametrize("text", ["password: AbCd1234\n  - item\n", "PASSWORD=abc\n  indented\n"])
def test_a_list_item_or_a_dotenv_line_does_not_fold(text: str) -> None:
    assert _redact(text).endswith(text.split("\n", 1)[1])


def test_a_secret_in_an_inline_image_text_chunk_is_found_without_the_redactor() -> None:
    import base64
    import struct
    import zlib

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    text_chunk = b"Comment\x00aws_secret_access_key=" + _KEY.encode()
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\x00" * 13) + chunk(b"tEXt", text_chunk)
    text = "![x](data:image/png;base64," + base64.b64encode(png).decode() + ")"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scan, "_CANONICAL_REDACTOR", None)
        assert scan.scan_text(text, "prompt") != []


# ── Round on 2550983e7a (GPT): long values past the stream's hold ──

_LONG = "Q7w" * 1700


@pytest.mark.parametrize(
    "text,tail",
    [
        ('db:\n  password: "' + _LONG + '\n    Rtv8Kk3LmTail"\n  user: x\n', "  user: x\n"),
        ("db:\n  password: '" + _LONG + "\n    Rtv8Kk3LmTail'\n  user: x\n", "  user: x\n"),
        ('password = """' + _LONG + '\nRtv8Kk3LmTail"""\nnext = 1\n', "next = 1\n"),
        ("<password><![CDATA[" + _LONG + "<Rtv8Kk3LmTail]]></password> ok\n", "</password> ok\n"),
    ],
)
@pytest.mark.parametrize("size", [1, 64, 1024])
def test_a_long_value_past_the_hold_drops_through_its_own_close(
    text: str, tail: str, size: int
) -> None:
    for out in (_stream(text, size), _redact(text), SecretRedactionFilter([]).redact(text)):
        assert "Rtv8" not in out and "Q7wQ7wQ7w" not in out
    assert _stream(text, size).endswith(tail)


# ── Round on a7238fa530 (GPT): quoted config values behind weak labels ──


@pytest.mark.parametrize(
    "text", ['DB_PASS="AbCdEfG"\n', "DB_PASS='AbCdEfG'\n", 'db_pass: "AbCdEfG"\n']
)
@pytest.mark.parametrize("size", [1, 7])
def test_a_quoted_config_value_behind_a_weak_label_is_a_literal(text: str, size: int) -> None:
    from kiro_crew.deploy.scan import scan_content

    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "AbCd" not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


@pytest.mark.parametrize("text", ['{"pwd": "deny"}', 'x lane_pass="deny"'])
def test_a_quoted_word_off_a_config_line_keeps_the_weak_label_tests(text: str) -> None:
    assert _redact(text) == text


# ── Round on 5d8d17838c (GPT): short quoted passwords behind weak labels ──


@pytest.mark.parametrize("text", ['{"db_pass": "Ab1$cD"}', '{"pwd": "x9!Q"}'])
@pytest.mark.parametrize("size", [1, 7])
def test_a_short_quoted_password_behind_a_weak_label_is_redacted(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Ab1$cD" not in out and "x9!Q" not in out


@pytest.mark.parametrize("text", ['{"pwd": "deny"}', '{"pwd": "on"}', '{"pwd": ""}'])
def test_a_quoted_word_behind_a_weak_label_is_kept(text: str) -> None:
    assert _redact(text) == text


# ── Round on 903e7a216e (GPT): long folded YAML values, vendor identifiers ──


@pytest.mark.parametrize("size", [7, 64, 1024])
def test_a_long_folded_yaml_value_keeps_its_label_to_its_end(size: int) -> None:
    body = "".join("  Fold%02d" % i + "x" * 92 + "\n" for i in range(70))
    text = "db:\npassword: AbCd1234" + "y" * 90 + "\n" + body + "next: ok\n"
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Fold" not in out and "AbCd" not in out and out.endswith("next: ok\n")


_TWILIO_SID = "SK" + "0123456789abcdef" * 2


@pytest.mark.parametrize(
    "text", ['{"twilio_api_key": "' + _TWILIO_SID + '"}', "TWILIO_API_KEY=" + _TWILIO_SID + "\n"]
)
def test_a_vendor_identifier_keeps_its_tier_behind_a_generic_label(text: str) -> None:
    from kiro_crew.deploy.scan import scan_content

    assert _redact(text) == text
    assert [f for f in scan_content(text) if f.severity == "credential"] == []


def test_the_same_string_behind_a_password_label_is_still_a_secret() -> None:
    assert _TWILIO_SID not in _redact("password: " + _TWILIO_SID + "\n")


# ── Round on c51a1758e8 (GPT): template literals, name/value in any order ──

_JS_KEY = "Xq7m2pz9RtvAbCdEfGh1234JkLmNoPqRsTuVwXyZ"


@pytest.mark.parametrize(
    "text",
    ["const password = `" + _JS_KEY + "`;", "const password = `Xq7m\\`2pz9\nRtvAbCd`;\nnext()"],
)
@pytest.mark.parametrize("size", [1, 7])
def test_a_backtick_password_is_read_to_its_closing_backtick(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m" not in out and "RtvAbCd" not in out


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "AWS_SECRET_ACCESS_KEY", "type": "PLAINTEXT", "value": _JS_KEY},
        {"value": _JS_KEY, "name": "DB_PASSWORD"},
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_name_and_value_pair_in_any_order_is_one_secret(entry: dict, size: int) -> None:
    from kiro_crew.deploy.scan import scan_content

    text = json.dumps({"environmentVariables": [entry]}, sort_keys=True)
    for out in (_redact(text), _stream(text, size)):
        assert _JS_KEY[:10] not in out
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


def test_a_value_beside_an_ordinary_name_is_kept() -> None:
    text = json.dumps({"env": [{"value": "production", "name": "STAGE"}]})
    assert _redact(text) == text


# ── Round on dea0017849 (GPT): sibling pairs in the stream, braces, case ──


@pytest.mark.parametrize(
    "value", ["correct horse battery staple", "p@ss w0rd!", "abc", "Tr0ub4dor&3", "ab{cd}ef"]
)
@pytest.mark.parametrize("size", [1, 3, 7, 64])
def test_a_value_before_its_secret_name_never_streams(value: str, size: int) -> None:
    text = '{"value":"' + value + '", "name":"DB_PASSWORD"}'
    assert value not in _stream(text, size)
    assert value not in _redact(text)


def test_upper_case_sibling_fields_are_one_secret() -> None:
    from kiro_crew.deploy.scan import scan_content

    text = '{"VALUE":"' + _JS_KEY + '","NAME":"AWS_SECRET_ACCESS_KEY"}'
    assert _JS_KEY[:10] not in _redact(text)
    assert [f for f in scan_content(text) if f.severity == "credential"] != []


def test_each_object_keeps_its_own_name() -> None:
    text = json.dumps(
        [{"name": "STAGE", "value": "production"}, {"value": "Xq7m2pz9Rtv", "name": "DB_PASSWORD"}]
    )
    out = _redact(text)
    assert '"production"' in out and "Xq7m2pz9Rtv" not in out


# ── Round on 1274e13bbf (GPT): sibling fields far apart, long backtick values ──

_AWS_K = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [
        '{"name":"SecretAccessKey","description":"'
        + "lorem ipsum " * 67
        + '","value":"'
        + _AWS_K
        + '"} more text after',
        '{"value":"'
        + _AWS_K
        + '","description":"'
        + "lorem ipsum " * 67
        + '","name":"SecretAccessKey"} more',
    ],
)
@pytest.mark.parametrize("size", [7, 64, 1024])
def test_sibling_fields_far_apart_never_stream_apart(text: str, size: int) -> None:
    out = _stream(text, size)
    assert "wJalr" not in out and out.endswith(" more" if text.endswith(" more") else "after")


@pytest.mark.parametrize("size", [1, 64, 1024])
def test_a_long_backtick_value_drops_through_its_closing_backtick(size: int) -> None:
    text = "const password = `" + "Q7w" * 1734 + "\nRtv8Kk3LmTail`;\nnext()\n"
    out = _stream(text, size)
    assert "Rtv8" not in out and "Q7wQ7wQ7w" not in out and out.endswith("`;\nnext()\n")


# ── Round on 1e2b431549 (GPT): YAML list items ──


@pytest.mark.parametrize(
    "text,kept",
    [
        ("- password: |\n    DemoValue123\n  username: alice\n  enabled: true\n", "alice"),
        ("items:\n  - password: AbCd1234\n      EfGh5678\n    user: x\n", "user: x"),
        ('- password: "Xq7m\n    Rtv8"\n  user: x\n', "user: x"),
        (json.dumps({"c": "- password: |\n    DemoValue123\n  username: alice\n"}), "alice"),
        ('  "password": |\n    DemoValue123\n  username: alice\n', "alice"),
    ],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_list_item_value_ends_at_its_sibling_field(text: str, kept: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert kept in out
        assert not [w for w in ("DemoValue", "AbCd", "EfGh", "Xq7m", "Rtv8") if w in out]


# ── Round on a5358d77fb (GPT): later quoted fields, triple quotes opening a line ──


@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_later_quoted_field_does_not_set_a_folded_value_indent(size: int) -> None:
    text = 'db:\n  password: AbCd1234\n    EfGh5678\n  note: "      text"\n'
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "AbCd" not in out and "EfGh" not in out and out.endswith('  note: "      text"\n')


@pytest.mark.parametrize(
    "text",
    ['password = """\nXq7m2pz9Rtv\nAbCdEf"""\nnext = 1\n', "password = '''\nXq7m2pz9Rtv'''\n"],
)
@pytest.mark.parametrize("size", [1, 7, 64])
def test_a_triple_quoted_value_opening_on_the_next_line_is_read(text: str, size: int) -> None:
    for out in (_redact(text), _stream(text, size), SecretRedactionFilter([]).redact(text)):
        assert "Xq7m" not in out and "AbCdEf" not in out


# ── GPT on ce6a792ccf: sibling fields far apart in a view, ANSI-coloured keys ──

_SIBLING_SECRET = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize("gap", [800, 2000, 3500])
def test_a_distant_sibling_value_is_read_in_the_decoded_view(gap: int) -> None:
    """An escaped ``name`` key decodes in a window; the sibling ``value`` past a
    long description must fall inside the same window, in redaction and in
    the deploy scan."""
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    desc = ("lorem ipsum dolor sit amet " * 200)[:gap]
    text = '{"na\\u006de":"SecretAccessKey","description":"%s","value":"%s"}' % (
        desc,
        _SIBLING_SECRET,
    )
    out = _redact(text)
    assert _SIBLING_SECRET not in out
    assert desc in out
    assert any(is_credential_finding(f) for f in scan_content(text))


@pytest.mark.parametrize("size", [1, 7, 64])
def test_an_ansi_coloured_value_key_holds_its_sibling_secret_in_the_stream(size: int) -> None:
    """Terminal colour around the ``value`` key is dropped only in the decoded
    view; the stream's object hold must read that view too."""
    desc = ("x" * 9 + " ") * 36
    text = '{"\x1b[34mvalue\x1b[0m":"%s","description":"%s","name":"SecretAccessKey"}' % (
        _SIBLING_SECRET,
        desc,
    )
    out = _stream(text, size)
    assert _SIBLING_SECRET not in out
    assert _SIBLING_SECRET not in _redact(text)
    assert "SecretAccessKey" in out


# ── GPT on ab259497e4: short Authorization words, compound pass labels, dotted INI keys ──


@pytest.mark.parametrize(
    "text, secret",
    [
        ("GET /x HTTP/1.1\nAuthorization: Bearer abcdef\nHost: h\n", "abcdef"),
        ("Authorization: Basic ABCDEFG\n", "ABCDEFG"),
        ('{"db_pass": "lowercasep"}', "lowercasep"),
        ('{"admin.pass": "hunterpass"}', "hunterpass"),
        ("[database]\ndb.password = hunter_two_x\n", "hunter_two_x"),
    ],
)
def test_a_short_header_word_a_compound_pass_and_a_dotted_ini_key_are_redacted(
    text: str, secret: str
) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    outs = [_redact(text), SecretRedactionFilter([]).redact(text)]
    outs += [_stream(text, size) for size in (1, 7, 64)]
    assert all(secret not in out for out in outs)
    assert any(is_credential_finding(f) for f in scan_content(text))


@pytest.mark.parametrize(
    "text, kept",
    [
        ('{"pwd": "deny"}', "deny"),
        ("first_pass: completed\n", "completed"),
        ("Authorization: Bearer $TOKEN\n", "$TOKEN"),
        ("x self.password = new_password\n", "new_password"),
    ],
)
def test_the_word_reference_and_code_exemptions_still_hold(text: str, kept: str) -> None:
    assert kept in _redact(text)


# ── GPT on 435921e94a: deploy records placed by offset, unquoted compound pass ──


def test_a_password_naming_an_earlier_placeholder_word_is_a_hard_deploy_finding() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    text = 'const u = `${base}?sig=${sigvalue}`;\nconst cfg = {"password": "sigvalue"};\n'
    assert any(is_credential_finding(f) for f in scan_content(text))


def test_every_record_names_where_its_value_starts() -> None:
    from kiro_crew.security.redaction import redact_credentials_with_records

    text = "a password=Xq7m2pz9Rtv b password=Xq7m2pz9Rtv\n"
    records = redact_credentials_with_records(text)[2]
    assert [r.start for r in records] == [11, 34]
    assert all(text[r.start : r.start + len(r.value)] == r.value for r in records)


@pytest.mark.parametrize(
    "text, secret",
    [
        ("db_pass: Ab3x9Qz\n", "Ab3x9Qz"),
        ("config:\n  admin_pass: hunter7\n", "hunter7"),
        ("x db_pass=abcdefg more\n", "abcdefg"),
    ],
)
def test_an_unquoted_compound_password_is_redacted(text: str, secret: str) -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    outs = [_redact(text), SecretRedactionFilter([]).redact(text)]
    outs += [_stream(text, size) for size in (1, 7, 64)]
    assert all(secret not in out for out in outs)
    assert any(is_credential_finding(f) for f in scan_content(text))


@pytest.mark.parametrize(
    "text, kept",
    [
        ("first_pass: completed\n", "completed"),
        ("lane_pass: green\n", "green"),
        ("pwd: /usr/local\n", "/usr/local"),
    ],
)
def test_a_status_pass_label_and_a_working_directory_stay_visible(text: str, kept: str) -> None:
    assert kept in _redact(text)


# GPT on 9a5c48f0f6: a signed link nested percent-encoded in another value
# (``next=...%26sig%3D...``) kept its signature, because the decoded-view scan
# never read signing parameters.
_ENCODED_SAS_SIG = "Xq7m2pz9RtvAbCdEfGh1234567890abcdEFGH%2BkLm%3D"


@pytest.mark.parametrize(
    "text",
    [
        "see https://x/login?next=https%3A%2F%2Fa.blob.core.windows.net%2Fc%2Fb"
        f"%3Fsv%3D2022%26sr%3Db%26sig%3D{_ENCODED_SAS_SIG}%26se%3D2026 ok",
        f"url=https%3A%2F%2Fa.blob.core.windows.net%2Fc%3Fsv%3D2022%26sig%3D{_ENCODED_SAS_SIG} done",
        f"https%253A%252F%252Fa%253Fsv%253D1%2526sig%253D{_ENCODED_SAS_SIG}",
    ],
)
def test_a_percent_encoded_sas_signature_is_redacted(text: str) -> None:
    out = redact_credentials(text)[0]
    assert "Xq7m2pz9" not in out
    assert "sv%3D2022" in out or "sv%253D1" in out
    for size in (1, 7, 64):
        stream = StreamRedactor()
        streamed = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
        streamed += stream.flush()
        assert "Xq7m2pz9" not in streamed


def test_the_parameter_after_an_encoded_signature_stays() -> None:
    text = f"next=https%3A%2F%2Fa%2Fc%3Fsv%3D2022%26sig%3D{_ENCODED_SAS_SIG}%26se%3D2026"
    assert redact_credentials(text)[0].endswith("%26sig%3D[REDACTED: credential]%26se%3D2026")


# Opus on 41bc201beb, conductor ruling: a ``NAME=value;`` JavaScript statement
# inside an open ``{`` whose value is a member chain or call is code; a shell
# function's plain word and a dotenv line stay redacted.
def _streamed(text: str, size: int) -> str:
    stream = StreamRedactor()
    out = "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + stream.flush()


def test_a_js_member_chain_statement_in_a_block_is_kept() -> None:
    text = 'function f() {\n  password=document.getElementById("pw").value;\n}\n'
    assert redact_credentials(text)[0] == text
    for size in (1, 7, 64):
        assert _streamed(text, size) == text


@pytest.mark.parametrize(
    "text",
    ["f() { PASSWORD=hunter2; }\n", "f() {\n  PASSWORD=hunter2;\n}\n", "PASSWORD=x.y\n"],
)
def test_a_shell_function_or_dotenv_value_stays_redacted(text: str) -> None:
    assert "[REDACTED: credential]" in redact_credentials(text)[0]
    for size in (1, 7, 64):
        assert "[REDACTED: credential]" in _streamed(text, size)


def test_brace_counting_skips_quotes_in_linear_time() -> None:
    from kiro_crew import credential_values

    # Many escaped quotes after an opening one: each later quote is plain text.
    assert credential_values._braces_outside_quotes('"' + '\\"' * 4000 + "{") == ["{"]
    assert credential_values._braces_outside_quotes("'a{'}") == ["}"]
