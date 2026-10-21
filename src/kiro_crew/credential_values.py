"""Whether a value behind a secret label is a secret: the one shared predicate.

The batch redactor (:mod:`kiro_crew.security.redaction_labels`) and the log
filter (:mod:`kiro_crew.log_redaction`) both read a labelled value here, so the
two cannot drift apart. A value is a secret unless the WHOLE value is on a
short exemption list; nothing here guesses from a value's first characters or
from the code around it. Imports only the standard library and
:mod:`kiro_crew.credential_patterns`, so the log filter stays an import leaf.
"""

from __future__ import annotations

import contextvars
import functools
import re

from kiro_crew.credential_patterns import (
    AWS_KEY_ID_BODY,
    AWS_KEY_ID_PREFIXES,
    AWS_KEY_ID_REDACTION_ONLY_PREFIXES,
    REFERENCE_VALUE,
)

#: The shortest value a label rule redacts. Shorter values are flags, units and
#: words (``password: no``), not secrets.
MIN_VALUE_LEN = 8

#: How much of a quoted value the stream's window around a label covers: the
#: reach the view passes and the stream's tail are sized by.
QUOTED_MAX = 512

#: The longest quoted value read through to its closing quote, in every reader:
#: a long one (a session token, a password with commas) is removed whole.
QUOTED_READ_MAX = 4096

#: A one-case run of letters this long is a passphrase, not a word.
WORD_LEN_MAX = 15

_REFERENCE_RE = re.compile(REFERENCE_VALUE)

#: A whole redaction tag (``[REDACTED: credential]``): a fixed point, not a value.
#: Only the tags the redactors themselves write: any other text inside the
#: brackets is the writer's own, and may be a secret dressed as a tag.
_TAG_RE = re.compile(
    r"\[REDACTED(?:"
    r": (?:credential|encoded credential|policy request header"
    r"|unscannable JSON payload|suspicious URL)"
    r"| PRIVATE KEY|-(?:ACCESS|REFRESH|SSO))?\]"
)

#: The digest mask the MCP launch display writes for a hidden env value
#: (``KEY=<hidden sha256:0123abcd>``): a fixed point, as a tag is.
_DIGEST_MASK_RE = re.compile(r"<hidden sha256:[0-9a-f]{8}>")

#: A secret-store reference (``secret://name``, ``vault://path``, ``env:NAME``):
#: it names where a secret lives, and is not one.
#: A secret-store reference: a closed list of store schemes. A web URL
#: (``https://``) is not one: a webhook or a signed link authorizes by itself.
_STORE_REFERENCE_RE = re.compile(
    r"(?i:(?:vault|op|ssm|secretsmanager|aws-secretsmanager|keychain|keyring|env"
    r"|akv|azurekeyvault|gsm|gcpsm|sm|doppler|infisical|bws)://|(?:env|ssm|op|keychain):)"
)

#: A value that can only NAME something: an AWS key ID, a 12-digit account ID,
#: an ARN. Identifier tier, so it is never redacted behind a label.
NAMING_VALUE = (
    rf"(?:{AWS_KEY_ID_PREFIXES}|{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"
    r"|[0-9]{12}|arn:[a-z0-9-]+:\S*"
)
_NAMING_RE = re.compile(NAMING_VALUE, re.ASCII)


def value_extent(text: str, start: int, quote: str) -> int:
    """End of a value opened by *quote* at *start*: its unescaped closing quote.

    The extent depends on the quote state only. A value still open at the line
    end or the text end ends there, so it is removed whole up to that point
    rather than cut at its first delimiter. Past :data:`QUOTED_READ_MAX`
    characters the rest is read in one pass to the closing quote or the line
    end: a long value is never cut short and its tail released in the clear.
    """
    if quote == "`":
        # A template literal (a backtick-quoted value) runs over lines to its
        # unescaped closing backtick, or to the text end while it is open.
        pos = start
        while True:
            close = text.find("`", pos)
            if close == -1:
                return len(text)
            segment = text[start:close]
            if (len(segment) - len(segment.rstrip("\\"))) % 2 == 0:
                return close
            pos = close + 1
    if quote and text[max(0, start - 3) : start] == quote * 3:
        # A triple-quoted string (``password = """..."""``) runs over lines to
        # its own triple quote, or to the text end while it is still open.
        close = text.find(quote * 3, start)
        return len(text) if close == -1 else close
    if quote and text[start - 1 : start + 2] == quote * 3:
        # The same, read from its first quote (the other two still ahead).
        close = text.find(quote * 3, start + 2)
        return len(text) if close == -1 else close
    pos = start
    limit = min(len(text), start + QUOTED_READ_MAX)
    while pos < limit:
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == "'" == quote and pos > start and text[pos + 1 : pos + 2] == "'":
            # A doubled single quote is one quote inside the value (YAML, SQL:
            # ``'it''s'``), not its close; an empty ``''`` still closes at once.
            pos += 2
            continue
        if char == quote:
            return pos
        if char == "\n":
            # A YAML quoted scalar goes on over lines indented deeper than its
            # key's line (``password: "abc`` then ``  def"``): read on through
            # them. A line not indented deeper ends it at the break; a break at
            # the text end, the next line's indent not arrived, keeps it open.
            nxt = _continued_quote_line(text, start, pos)
            if nxt is None:
                return pos
            if nxt >= len(text):
                return len(text)
            pos = nxt
            continue
        pos += 1
    # Past the bound the rest is read in one pass per line, and a line break
    # still goes on with the value over an indented continuation line.
    while pos < len(text):
        rest = _QUOTED_REST_RE[quote].match(text, pos)
        pos = rest.end() if rest is not None else pos
        if pos >= len(text) or text[pos] != "\n":
            return pos
        nxt = _continued_quote_line(text, start, pos)
        if nxt is None:
            return pos
        pos = nxt
    return len(text)


def _continued_quote_line(text: str, start: int, brk: int) -> int | None:
    """Where the line after the break at *brk* starts its text, when it goes
    on a quoted value opened at *start*: it is indented deeper than the value's
    first line. ``len(text)`` when only blanks follow the break so far; None
    when the value ends at the break."""
    line_start = text.rfind("\n", 0, start) + 1
    first = text[line_start:start]
    indent = key_indent(first)
    # Blank lines inside the scalar are part of it (they fold to a line
    # break): the next line with text decides, as far as the read reaches.
    line = brk + 1
    while True:
        pos = line
        while pos < len(text) and text[pos] == " ":
            pos += 1
        if pos < len(text) and text[pos] == "\r":
            pos += 1
        if pos >= len(text):
            return len(text)
        if text[pos] != "\n":
            break
        line = pos + 1
        if line - brk > QUOTED_READ_MAX:
            return None
    if text[pos - 1] == "\r" or pos - line <= indent:
        return None
    return pos


#: The rest of a quoted value past :data:`QUOTED_READ_MAX`: escapes and any byte
#: but its quote or a line break.
_QUOTED_REST_RE = {q: re.compile(rf"(?:[^{q}\\\n]|\\.)*") for q in ('"', "`")}
_QUOTED_REST_RE["'"] = re.compile(r"(?:[^'\\\n]|\\.|'')*")


#: An XML CDATA section's opener.
_CDATA_OPEN = "<![CDATA["


def xml_value_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of the value of an XML element the label names (``<password>v</``):
    its closing ``</``, so punctuation inside the value is read as value. None
    when the label is no element or the element does not close on its line."""
    gt = value_start
    # Blanks and line breaks between the tag and its content (``<password>``
    # then the value on the next line) belong to the content.
    while gt > label_start and text[gt - 1] in " \t\r\n":
        gt -= 1
    if text[gt - 1 : gt] != ">":
        return None
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-:"):
        pos -= 1
    if text[pos - 1 : pos] != "<":
        return None
    # Read to the closing tag at any length on its line, as a quoted value is
    # read past QUOTED_READ_MAX: a long value is never cut at its punctuation.
    if text.startswith(_CDATA_OPEN, value_start):
        # A CDATA section is the element's content whatever it holds (``<``
        # too): the value runs through its ``]]>``, or to the text end while
        # it is still open, so no part of it is released.
        cdata_end = text.find("]]>", value_start + len(_CDATA_OPEN))
        return len(text) if cdata_end == -1 else cdata_end + 3
    # The value ends at the next ``<``, which must open the closing tag on the
    # same line: one search to it, so a run of labels reads each value once.
    close = text.find("<", value_start)
    if close == -1 or not text.startswith("</", close):
        return None
    return close


#: A YAML block-scalar header at a value: ``|`` or ``>``, an optional chomping
#: and indentation indicator, blanks and an optional comment, then the line end.
_BLOCK_HEADER_RE = re.compile(r"\A[|>](?:[-+][0-9]?|[0-9][-+]?)?[ \t]*(?:#[^\n]*)?\r?\n")


def _anchored_end(pattern: re.Pattern[str], text: str, start: int) -> int | None:
    """End of *pattern* (anchored with ``\\A``) matched on the line from *start*
    through its line break: each pattern is read on a bounded slice, never
    tried at every position of the text."""
    nl = text.find("\n", start)
    match = pattern.match(text[start : len(text) if nl == -1 else nl + 1])
    return None if match is None else start + match.end()


#: A line's indentation and YAML sequence indicators up to its key (``- ``,
#: ``  - - ``): the column its key sits at.
_KEY_INDENT_RE = re.compile(r"[ ]*(?:-[ ]+)*")


def key_indent(head: str) -> int:
    """The column a line's key sits at, sequence indicators counted: the key of
    ``- password: |`` is at column 2, so its body is indented deeper than 2 and
    a sibling field at column 2 (``  username: alice``) ends it."""
    # A key's own opening quote (``  "password": |``) is part of the key; text
    # up to an earlier quote is the string the YAML sits in (``{"c": "- pass``).
    head = head.rstrip("\"'")
    head = head[max(head.rfind('"'), head.rfind("'")) + 1 :]
    match = _KEY_INDENT_RE.match(head)
    return match.end() if match is not None else 0


def block_scalar_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of a YAML block scalar the label opens (``key: |`` and its indented
    lines): the end of its last body line. None when *value_start* holds no
    block header, or no body line follows it.

    The body is every following line that is blank or indented deeper than the
    label's own line, as YAML reads it."""
    header_end = _anchored_end(_BLOCK_HEADER_RE, text, value_start)
    if header_end is None:
        return None
    line_start = text.rfind("\n", 0, label_start) + 1
    head = text[line_start:label_start]
    prefix = LINE_PREFIX.get()
    if line_start == 0 and isinstance(prefix, str) and prefix is not _DECODED:
        head = prefix + head  # the part of the line a stream already emitted
    indent = key_indent(head)
    pos, end = header_end, None
    while pos < len(text):
        nl = text.find("\n", pos)
        line = text[pos : len(text) if nl == -1 else nl]
        if line.strip():
            if len(line) - len(line.lstrip(" ")) <= indent:
                break
            end = pos + len(line.rstrip())
        if nl == -1:
            break
        pos = nl + 1
    return end


#: A block-scalar header inside an escaped string, through its escaped break
#: (``|\\n``, ``>-\\r\\n``, the backslashes doubled one level deeper).
_ESC_BLOCK_HEADER_RE = re.compile(
    r"\A[|>](?:[-+][0-9]?|[0-9][-+]?)?[ \t]{0,64}(?:\\{1,8}r)?\\{1,8}n"
)
#: An escaped line break, and a quote no backslash escapes (the string's end).
_ESC_BREAK_RE = re.compile(r"(?<!\\)(?:\\{1,8}r)?\\{1,8}n")
_ESC_STRING_END_RE = re.compile(r'(?<!\\)(?:\\\\)*"')


def escaped_block_scalar_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of a YAML block scalar written in an escaped string
    (``"password: |\\n  body\\n"``): the end of its last body line, the body
    being the escaped lines indented deeper than the label's, up to the
    string's closing quote. None when *value_start* holds no escaped header."""
    header = _ESC_BLOCK_HEADER_RE.match(text[value_start : value_start + 128])
    if header is None:
        return None
    header_end = value_start + header.end()
    line_start = max(
        (m.end() for m in _ESC_BREAK_RE.finditer(text, max(0, label_start - 256), label_start)),
        default=0,
    )
    line_start = max(line_start, text.rfind('"', 0, label_start) + 1)
    head = text[line_start:label_start]
    indent = key_indent(head)
    close = _ESC_STRING_END_RE.search(text, header_end)
    stop = len(text) if close is None else close.start()
    pos, end = header_end, None
    while pos < stop:
        brk = _ESC_BREAK_RE.search(text, pos, stop)
        line = text[pos : stop if brk is None else brk.start()]
        if line.strip():
            if len(line) - len(line.lstrip(" ")) <= indent:
                break
            end = pos + len(line.rstrip())
        if brk is None:
            break
        pos = brk.end()
    return end


#: The shell's working-directory variables (``PWD=/srv/app``): ``pwd`` there
#: names a directory, not a password.
_WORKING_DIR_VAR_RE = re.compile(r"(?<![A-Za-z0-9_])(?:OLD)?PWD[ \t]*[=:][ \t]*\Z")


def names_working_dir(label: str) -> bool:
    """Whether *label* (with its separator) is the shell's ``PWD``/``OLDPWD``."""
    return _WORKING_DIR_VAR_RE.search(label) is not None


#: What precedes a URL query parameter's name: ``?``, ``&``, their percent
#: codes, or an HTML-escaped ``&``.
_QUERY_LEAD_RE = re.compile(r"(?:[?&]|%3[Ff]|%26|&amp;|&#0{0,4}38;|&#[xX]0{0,4}26;)\Z")


def in_url_query(text: str, label_start: int) -> bool:
    """Whether the label word at *label_start* names a URL query parameter
    (``?password=...``): its value is data, a literal, never code."""
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    lead = text[max(0, pos - 8) : pos]
    prefix = LINE_PREFIX.get()
    if pos < 8 and prefix and prefix is not _DECODED:
        # A stream window that starts mid-line: what the line already emitted
        # holds the ``?`` or ``&`` before the name.
        lead = prefix[-8:] + lead
    return _QUERY_LEAD_RE.search(lead) is not None


def in_url_authority(text: str, label_start: int) -> bool:
    """Whether the label word at *label_start* is a URL's user name
    (``https://x-access-token:ghp_...@github.com``): its value ends at the
    ``@`` that opens the host."""
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    return text[max(0, pos - 3) : pos] == "://"


def url_value_end(text: str, label_start: int, value_start: int, end: int) -> int:
    """Where a labelled value in a URL ends: at the ``@`` before the host for a
    user name's password, and at the ``#`` fragment for a query value; *end*
    otherwise."""
    if in_url_authority(text, label_start):
        cut = text.find("@", value_start, end)
        return cut if cut > value_start else end
    if in_url_query(text, label_start):
        cut = text.find("#", value_start, end)
        return cut if cut > value_start else end
    return end


def in_closing_tag(text: str, label_start: int) -> bool:
    """Whether the label word at *label_start* names a closing XML tag
    (``</password>``): no value follows it."""
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-:"):
        pos -= 1
    return text[max(0, pos - 2) : pos] == "</"


def closes_label_literal(text: str, label_start: int, value_start: int) -> bool:
    """Whether the quote right before *value_start* closes a string literal the
    label word sits in (``getpass("Password:")``, ``unknown key "api_key:"``)
    rather than opening a value: the same quote stands before the label's word
    and nothing between closes it. ``"password": "x"`` closes its key first."""
    quote = text[value_start - 1 : value_start]
    if quote not in ('"', "'"):
        return False
    if (
        isinstance(LINE_PREFIX.get(), _ViewPrefix) or LINE_PREFIX.get() is _DECODED
    ) and _LITERAL_CLOSE_FOLLOW_RE.match(text, value_start) is None:
        # A normalised view has decoded its escapes: ``"password=\"x\""`` reads
        # as ``"password="x""``, and which quote was escaped is gone. Only code
        # punctuation after the quote (``getpass("Password:")``) closes a
        # literal there; anything else opens a value. The raw text keeps the
        # escapes and is judged by them below.
        return False
    pos = label_start
    while pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    if text[pos - 1 : pos] != quote or quote in text[pos : value_start - 1]:
        return False
    # The two quotes close one literal only when they are escaped alike: an
    # escaped ``\"`` before the value inside a plain ``"`` string opens a value.
    return _backslashes_before(text, pos - 1) == _backslashes_before(text, value_start - 1)


#: What may follow a quote that closes a label literal in a decoded view: only
#: code punctuation up to a blank, a quote or the text end (``)``, ``, ``),``);
#: a letter or digit in the run makes it a value (``)bc9...``).
_LITERAL_CLOSE_FOLLOW_RE = re.compile(r"""[),;\]}]{0,8}(?:[\s"'`]|\Z)""")


def _backslashes_before(text: str, pos: int) -> int:
    """How many ``\\`` stand right before *pos*."""
    run = 0
    while pos - run > 0 and text[pos - run - 1] == "\\":
        run += 1
    return run


def is_tag(value: str) -> bool:
    """Whether *value* is a whole redaction tag."""
    return _TAG_RE.fullmatch(value) is not None


def without_tags(text: str, start: int, end: int) -> str:
    """``text[start:end]`` with every whole redaction tag that starts in it
    removed: what is left is the writer's own text, a forged tag included."""
    out: list[str] = []
    pos = start
    for tag in _TAG_RE.finditer(text, start, len(text)):
        if tag.start() >= end:
            break
        out.append(text[pos : tag.start()])
        pos = max(pos, tag.end())
    if pos < end:
        out.append(text[pos:end])
    return "".join(out)


def is_naming_value(value: str) -> bool:
    """True when the WHOLE value is a key ID, an account ID or an ARN: names only."""
    return _NAMING_RE.fullmatch(value) is not None


def _is_dotted_name(value: str) -> bool:
    """A chain of two or more names of letters and ``_`` (``settings.aws_key``)."""
    parts = value.split(".")
    return len(parts) > 1 and all(p and p.isascii() and p.replace("_", "").isalpha() for p in parts)


#: Labels whose word also names something that is not a secret: ``pwd`` (a
#: working directory, a shell command), a glued ``_pass`` / ``_token`` and a bare
#: ``signature``. A quoted value behind one keeps the length and word
#: exemptions (``{"bash": {"pwd": "deny"}}``); behind a strong label it does not.
_WEAK_LABEL_RE = re.compile(
    r"(?i:pwd|[_. -]pass|[_. -]token|signature)[\"'*_\s:=>\uff1a\uff1d\\]*\Z"
)


#: The AWS labels that end like a weak one (``aws_session_token``): never weak.
_AWS_TAIL_RE = re.compile(r"(?i:(?:session|security)[ _.-]?token)[\"'*_\s:=>\uff1a\uff1d\\]*\Z")


#: A compound password label: ``pass`` after a word naming an account or a store
#: (``db_pass``, ``admin.pass``, ``mysql_root_pass``). It is strong, quoted or
#: not; any other ``*_pass`` (``lane_pass: 'green'``, ``first_pass``) names a
#: status and keeps the weak tests.
_PASS_TAIL_RE = re.compile(
    r"(?i:(?<![A-Za-z0-9])"
    r"(?:db|database|admin|root|user|login|account|acct|mysql|mariadb|postgres|pg|psql"
    r"|redis|mongo|mongodb|ldap|smtp|imap|pop3|mail|ftp|sftp|ssh|vpn|wifi|sql|mssql"
    r"|oracle|rabbitmq|amqp|kafka|proxy|keystore|truststore|store|service|svc|app|api"
    r"|basic|http|auth|bind)[_. -]pass)[\"'*_\s:=>\uff1a\uff1d\\]*\Z"
)


def is_weak_label(label: str) -> bool:
    """Whether *label* (with its separator) is a weak label (:data:`_WEAK_LABEL_RE`);
    an AWS label and a compound password label (:data:`_PASS_TAIL_RE`) are never
    weak. Pass the label with the text before it: the matched label may be only
    its tail (``_pass: ``), the account word (``db``) sits in front."""
    if _PASS_TAIL_RE.search(label) is not None:
        return False
    return _WEAK_LABEL_RE.search(label) is not None and _AWS_TAIL_RE.search(label) is None


#: The shortest digit run behind a secret label that is a PIN.
MIN_PIN_LEN = 4

_UNICODE_ESCAPES_RE = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")

#: A code name that carries a secret label's word (``new_password``,
#: ``password_hash``, ``userApiKey``): lower-case first, letters and ``_`` only,
#: with a ``_`` or a capital inside, so it reads as an identifier.
_LABEL_WORD_NAME_RE = re.compile(r"[a-z_][A-Za-z_]*")
_LABEL_WORDS_RE = re.compile(r"(?i:passw(?:or)?d|passphrase|secret|token|api_?key|pwd)")


def _is_label_word_name(value: str) -> bool:
    """Whether *value*, written unquoted, is a variable named after the secret
    it holds (``password = new_password``): a name for the value, not the value."""
    return (
        _LABEL_WORD_NAME_RE.fullmatch(value) is not None
        and ("_" in value or not value.islower())
        and _LABEL_WORDS_RE.search(value) is not None
    )


def _is_word(value: str) -> bool:
    """A single-case word of letters at most :data:`WORD_LEN_MAX` long."""
    return value.isalpha() and (value.islower() or value.isupper()) and len(value) <= WORD_LEN_MAX


def is_exempt_value(
    value: str, *, aws: bool = False, quoted: bool = False, weak: bool = False
) -> bool:
    """Whether the WHOLE *value* names or masks a secret instead of holding one.

    The list: shorter than :data:`MIN_VALUE_LEN` (not a PIN of
    :data:`MIN_PIN_LEN` or more digits); a code name carrying the label's word
    (``new_password``, ``userApiKey``); a reference or a mask
    (``$VAR``, ``${VAR}``, ``%VAR%``, ``{{ x }}``, ``<your key>``, ``****``); a
    secret-store reference; a redaction tag; a single-case word of letters no
    longer than :data:`WORD_LEN_MAX` (``password: required``); a dotted chain of
    letter names (``settings.aws_key``); a path (``/srv/app``, ``~/x``, ``./x``).
    Every test is on the whole value. A *quoted* value is a literal, so only a
    reference, a store reference or a tag exempts it, whatever its length.
    """
    if not value or is_tag(value):
        return True
    if _REFERENCE_RE.fullmatch(value) is not None or _STORE_REFERENCE_RE.match(value):
        return True
    if quoted:
        # A quoted value behind a secret label is a literal the writer chose to
        # quote: no length, word, dotted-name or path test applies to it.
        return False
    if not any(c.isalnum() for c in value):
        # No letter or digit at all (``==``, ``->``): an operator, not a value.
        return True
    if _NULL_OR_BOOL_RE.fullmatch(value):
        # A language's null or boolean literal (``password = null``, ``"token":
        # false``): no value is set. A config line reads its value as a literal
        # before this is reached, so ``PASSWORD=false`` there is still redacted.
        return True
    if weak:
        # A weak label (``pwd``, a glued ``_token``) also names things that are
        # not secrets: a short value or a plain word behind it stays.
        if len(value) < MIN_VALUE_LEN:
            # A run of four or more digits is a PIN, not a flag or a unit.
            return not (len(value) >= MIN_PIN_LEN and value.isdigit())
        if _is_word(value) or _is_label_word_name(value):
            return True
    # Behind a strong label only a value that is visibly code names the secret
    # instead of holding it (``load_key()``, ``settings.key``, ``env["K"]``,
    # ``new_password``, ``userKey``, ``$VAR``): a plain word is a password at any
    # length or case. Behind an AWS label not even an identifier is code.
    if aws:
        return _is_dotted_name(value)
    return _CODE_VALUE_RE.fullmatch(value) is not None or (
        _PATH_VALUE_RE.fullmatch(value) is not None
    )


#: YAML's null and boolean literals (the core schema).
_YAML_NULL_OR_BOOL_RE = re.compile(r"~|null|Null|NULL|true|True|TRUE|false|False|FALSE")
#: A YAML mapping's separator right before a value: ``:`` and blanks.
_YAML_SEP_RE = re.compile(r":[ \t]+\Z")


def yaml_value(text: str, value_start: int) -> bool:
    """Whether the value at *value_start* follows a YAML ``key:`` separator."""
    return _YAML_SEP_RE.search(text, max(0, value_start - 16), value_start) is not None


#: The type names an annotation puts after a label, a closed list. A generic
#: wrapper (``Optional[str]``) is read as a subscript already.
_TYPE_NAME_RE = re.compile(
    r"str|string|bytes|int|number|bool|boolean|any|unknown|object|None|null|undefined"
    r"|Optional|SecretStr"
)
#: A minified name: one character.
_MINIFIED_NAME_RE = re.compile(r"[A-Za-z_$]")
#: What may follow a type or a minified name: code punctuation, a blank or the
#: line end (``""`` is the text end).
_TYPE_OR_NAME_FOLLOW = frozenset(",)=;}]>| \t\r\n") | {""}

#: The keywords that open an expression with an operand after a blank.
_OPERAND_KEYWORD_RE = re.compile(r"await|new|yield|typeof|lambda")

#: The null and boolean literals of the languages and formats a label sits in.
_NULL_OR_BOOL_RE = re.compile(r"null|NULL|None|nil|undefined|true|false|True|False")

#: A value that is visibly code, not a secret: a call (``load_key()``), a dotted
#: or indexed name (``settings.key``, ``env["K"]``), an identifier with a ``_``
#: or a capital inside (``new_password``, ``userKey``), or a variable reference.
_CODE_VALUE_RE = re.compile(
    r"\A(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|[A-Za-z_]+(?:\.[A-Za-z_]+)+"
    r"|[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\]\n]{0,256}\]|\([^)\n]{0,256}\))"
    # snake_case or SNAKE_CASE words, and camelCase words: a capital is followed
    # by lower case, so a random token's mixed case does not read as one.
    r"|_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+|_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|[a-z]+(?:[A-Z][a-z]+)+)\Z"
)

#: The only literal a config line exempts: a variable reference, exactly.
_CONFIG_REFERENCE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

#: A config line up to its value: indentation, an ``export`` or a YAML list
#: dash, the key, and ``=`` or ``:`` with blanks around it (dotenv, ini, shell,
#: YAML, TOML).
_CONFIG_PREFIX_RE = re.compile(r"[ \t]*(?:export[ \t]+|-[ \t]+)?")
#: The key and its separator: ``NAME=`` with nothing around the ``=``, or
#: ``key:`` and blanks (YAML).
_CONFIG_KEY_SEP_RE = re.compile(r"\A[A-Za-z0-9_-]+(?:=|:[ \t]+)\Z")
#: The next logfmt field after a config value: blanks, then ``name=``.
_LOGFMT_NEXT_FIELD_RE = re.compile(r"(?<=[ \t])[A-Za-z_][A-Za-z0-9_.-]{0,128}=")
#: The logfmt fields before a later one on its line (``level=info ``): each a
#: ``name=`` and an unquoted or double-quoted value, then blanks. A value with
#: code punctuation (``a=1, ``, ``f(a=1``) makes it code, not logfmt.
_LOGFMT_PREFIX_RE = re.compile(
    r"[ \t]*(?:[A-Za-z_][A-Za-z0-9_.-]{0,128}=(?:\"(?:[^\"\\\n]|\\.)*\"|[^\s\"=,;()]*)[ \t]+){1,64}"
)
#: How many continued lines (``\`` at a line end) a config value reads on.
_CONFIG_CONTINUATION_MAX = 8
#: Where a config value's trailing comment starts: a blank, then ``#``.
_CONFIG_COMMENT_RE = re.compile(r"[ \t]#")


#: What a line already holds before the text being read starts: ``""`` when
#: the text starts a line (the default), the part of the line a stream already
#: emitted, or ``None`` when that is not known. A config line's start is judged
#: on it, so a stream window reads the line as the whole text would.
LINE_PREFIX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "config_line_prefix", default=""
)

#: :data:`LINE_PREFIX` while a decoded view is read (``password%3Da,b``): its
#: separators came from an encoding, so no line in it is a config file's line.
_DECODED = "\x00decoded"


#: Whether the text before the text being read ends inside an INI section (a
#: ``[section]`` line, then only keys, comments and blank lines): a stream's
#: emitted lines, which are outside the window being read.
INI_SECTION: contextvars.ContextVar[bool] = contextvars.ContextVar("ini_section", default=False)

#: An INI section header, a whole line.
_INI_SECTION_RE = re.compile(r"\A[ \t]*\[[^\]\n]{1,128}\][ \t]*\r?\Z")
#: A line an INI section goes on through: blank, a comment, or a key.
_INI_BODY_RE = re.compile(r"\A[ \t]*(?:[;#]|[A-Za-z0-9_.-]+[ \t]*[=:]|\r?\Z)")
#: An INI key and its ``=`` with blanks around it (``password = value``).
_INI_KEY_SEP_RE = re.compile(r"\A[A-Za-z0-9_.-]+[ \t]*=[ \t]*\Z")


def ini_line_state(in_section: bool, line: str) -> bool:
    """Whether an INI section is open after *line*, given *in_section* before it."""
    if _INI_SECTION_RE.match(line) is not None:
        return True
    return in_section and _INI_BODY_RE.match(line) is not None


def _in_ini_section(text: str, line_start: int) -> bool:
    """Whether the line at *line_start* is inside an INI section: a header
    above it with only keys, comments and blank lines between. ``password =
    new_password`` in code has no header above it, so it stays code."""
    prefix = LINE_PREFIX.get()
    lead = prefix if isinstance(prefix, str) and prefix is not _DECODED else ""
    return line_start in _ini_section_lines(text, lead, INI_SECTION.get())


@functools.lru_cache(maxsize=2)
def _ini_section_lines(text: str, lead: str, in_section: bool) -> frozenset[int]:
    """The starts of the lines of *text* inside an INI section, read in one
    pass: *in_section* is the state before the text, *lead* the part of its
    first line already emitted."""
    inside: set[int] = set()
    pos = 0
    while pos <= len(text):
        if in_section:
            inside.add(pos)
        nl = text.find("\n", pos)
        if nl == -1:
            break
        line = (lead if pos == 0 else "") + text[pos:nl]
        in_section = ini_line_state(in_section, line)
        pos = nl + 1
    return frozenset(inside)


def reading_decoded() -> contextvars.Token[str | None]:
    """Mark the text read until the returned token is reset as a decoded view
    whose line context is unknown: no line in it is a config line."""
    return LINE_PREFIX.set(_DECODED)


class _ViewPrefix(str):
    """:data:`LINE_PREFIX` while a normalised view is read: a ``\\`` there may
    be a decoded escape (``\\\\`` read as one), so no line is continued by it."""


def reading_view(source: str, lo: int) -> contextvars.Token[str | None]:
    """Read a normalised view of ``source[lo:]`` until the returned token is
    reset: its first line goes on the source line that *lo* is on, so a view
    line starts a config line exactly when the source line does there, and a
    line break the view decoded (``%0A``) starts one as a written one would."""
    line_start = source.rfind("\n", 0, lo) + 1
    if line_start:
        return LINE_PREFIX.set(_ViewPrefix(source[line_start:lo]))
    outer = LINE_PREFIX.get()
    if outer is None or outer is _DECODED:
        return LINE_PREFIX.set(_DECODED)
    return LINE_PREFIX.set(_ViewPrefix(outer + source[:lo]))


def continues_line(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` ends in a ``\\`` no other escapes: a line
    continued on the next one."""
    run = len(text[start:end]) - len(text[start:end].rstrip("\\"))
    return run % 2 == 1


def _config_key_start(text: str, line_start: int, label_start: int) -> int:
    """Where the key that ends in the label at *label_start* starts."""
    key_start = label_start
    # A dotted key (``db.password``) is one key, as :data:`_INI_KEY_SEP_RE` reads it.
    while key_start > line_start and (
        text[key_start - 1].isalnum() or text[key_start - 1] in "_-."
    ):
        key_start -= 1
    return key_start


def _config_line_start(text: str, label_start: int, value_start: int) -> int | None:
    """The start of the config line the label at *label_start* opens, its key
    and separator ending at *value_start* (dotenv, ini, YAML, an INI section's
    padded ``key = ``); None when it opens no such line."""
    if LINE_PREFIX.get() is _DECODED:
        return None
    line_start = text.rfind("\n", 0, label_start) + 1
    key_start = _config_key_start(text, line_start, label_start)
    key_sep = text[key_start:value_start]
    if line_start == 0:
        prefix = LINE_PREFIX.get()
        if prefix is None:
            return None
        lead = prefix + text[:key_start]
    else:
        lead = text[line_start:key_start]
    if _CONFIG_PREFIX_RE.fullmatch(lead) is None and (
        # A later logfmt field (``level=info password=...``) is a config value too.
        not key_sep.endswith("=")
        or _LOGFMT_PREFIX_RE.fullmatch(lead) is None
    ):
        return None
    if _CONFIG_KEY_SEP_RE.match(key_sep) is None and (
        _INI_KEY_SEP_RE.match(key_sep) is None or not _in_ini_section(text, line_start)
    ):
        return None
    return line_start


def quoted_config_value(text: str, label_start: int, value_start: int) -> bool:
    """Whether the quoted value at *value_start* is a config line's value
    (``DB_PASS="..."``, ``password: '...'``): a literal, as the unquoted form is."""
    if text[value_start - 1 : value_start] not in ('"', "'"):
        return False
    return _config_line_start(text, label_start, value_start - 1) is not None


#: How far back :func:`in_open_brace` reads for the ``{`` a line is inside.
OPEN_BRACE_REACH = _OPEN_BRACE_REACH = 4096


#: The text a stream emitted before the text being read, its last
#: :data:`_OPEN_BRACE_REACH` characters: the ``{`` a line is inside may be there.
BRACE_PREFIX: contextvars.ContextVar[str] = contextvars.ContextVar("brace_prefix", default="")


def in_open_brace(text: str, pos: int) -> bool:
    """Whether *pos* is inside a ``{`` that no ``}`` before it closes, within
    :data:`_OPEN_BRACE_REACH`: a line there is a member of an object or a
    block of code. Braces inside a quoted string on its own line are skipped."""
    lo = max(0, pos - _OPEN_BRACE_REACH)
    lead = (
        BRACE_PREFIX.get()[-(_OPEN_BRACE_REACH - pos) :]
        if lo == 0 and pos < _OPEN_BRACE_REACH
        else ""
    )
    if text.find("{", lo, pos) == -1 and "{" not in lead:
        return False
    depth = 0
    braces = _braces_outside_quotes(lead + text[lo:pos])
    for char in reversed(braces):
        if char == "}":
            depth += 1
        elif depth == 0:
            return True
        else:
            depth -= 1
    return False


_BRACE_RE = re.compile(r"[{}]")


#: A brace or a quote, the only characters the brace count reads.
_BRACE_OR_QUOTE_RE = re.compile(r"[{}\"']")


def _braces_outside_quotes(segment: str) -> list[str]:
    """The braces of *segment* outside a quoted run on one line (a ``\\``
    escapes the next character). Linear: a quote that no same quote closes on
    its line is plain text, and so is every later quote of that kind on the
    line, because its scan would read the same characters."""
    braces: list[str] = []
    dead = {'"': -1, "'": -1}
    pos = 0
    while True:
        m = _BRACE_OR_QUOTE_RE.search(segment, pos)
        if m is None:
            return braces
        char, at = m.group(), m.start()
        pos = at + 1
        if char in "{}":
            braces.append(char)
            continue
        if at < dead[char]:
            continue
        j = at + 1
        while j < len(segment) and segment[j] != "\n":
            if segment[j] == "\\":
                if j + 1 >= len(segment) or segment[j + 1] == "\n":
                    j += 1
                    break
                j += 2
                continue
            if segment[j] == char:
                break
            j += 1
        if j < len(segment) and segment[j] == char:
            pos = j + 1
        else:
            dead[char] = j


#: A JavaScript member chain or call, with at least one ``.name``, ``(...)`` or
#: ``[...]`` after its first name (``document.getElementById("pw").value``).
_JS_CHAIN_RE = re.compile(
    r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\([^()\n]{0,256}\)|\[[^\[\]\n]{0,256}\])+"
)


def _js_statement_in_block(text: str, line_start: int, value_start: int) -> bool:
    """Whether ``NAME=value`` is a JavaScript statement, not a dotenv or shell
    line: inside an open ``{``, the line ends in ``;``, and the value is a
    member chain or a call. A shell function's ``PASSWORD=hunter2;`` holds a
    plain word, so it stays a config line."""
    line_end = text.find("\n", value_start)
    line_end = len(text) if line_end == -1 else line_end
    statement = text[value_start:line_end].rstrip()
    if not statement.endswith(";"):
        return False
    if _JS_CHAIN_RE.fullmatch(statement[:-1].rstrip()) is None:
        return False
    return in_open_brace(text, line_start)


def config_value_end(text: str, label_start: int, value_start: int) -> int | None:
    """End of an unquoted value on a config line: ``NAME=value`` at the line
    start with nothing around the ``=`` (dotenv, ini, shell, an ``export``
    allowed), or ``key: value`` (YAML, a list ``- `` allowed). The value runs to
    the line end or to a ``#`` comment after a blank, through ``,`` and ``;``,
    trailing blanks dropped. None when the label does not start such a line or
    the value is quoted. ``key = value`` with blanks around the ``=`` is code
    (``password = new_password``), not this form."""
    if text[value_start - 1 : value_start] in ('"', "'") or value_start >= len(text):
        return None
    line_start = _config_line_start(text, label_start, value_start)
    if line_start is None:
        return None
    key_sep = text[_config_key_start(text, line_start, label_start) : value_start]
    if ":" in key_sep and in_open_brace(text, line_start):
        # ``key: value`` inside an open ``{`` is an object property in code
        # (``{\n  password: input.value,\n}``), not a YAML line: its unquoted
        # value is an expression, read as code is.
        return None
    if "=" in key_sep and _js_statement_in_block(text, line_start, value_start):
        return None
    pos = value_start
    # A ``\`` at the line end continues the value on the next line (shell,
    # dotenv): ``SECRET_ACCESS_KEY=\`` then the key.
    for _ in range(_CONFIG_CONTINUATION_MAX + 1):
        line_end = text.find("\n", pos)
        line_end = len(text) if line_end == -1 else line_end
        comment = _CONFIG_COMMENT_RE.search(text, pos, line_end)
        end = comment.start() if comment is not None else line_end
        end = pos + len(text[pos:end].rstrip())
        # logfmt (``password=abc user=bob``): a blank, then ``name=``, starts
        # the next field, since a logfmt value with a blank in it is quoted.
        # A line of one assignment still reads to its end (``PASSWORD=a b c``).
        if isinstance(LINE_PREFIX.get(), _ViewPrefix):
            # In a decoded view an ``&`` joins query parameters
            # (``key%3D<value>&account=...``): the value ends there.
            amp = text.find("&", pos, end)
            if amp != -1:
                end = amp
        field = _LOGFMT_NEXT_FIELD_RE.search(text, pos, end)
        if field is not None and field.start() > pos:
            end = pos + len(text[pos : field.start()].rstrip(" \t"))
            break
        if (
            comment is not None
            or line_end == len(text)
            or isinstance(LINE_PREFIX.get(), _ViewPrefix)
            or not continues_line(text, pos, end)
        ):
            if comment is None and ":" in key_sep:
                end = folded_plain_end(
                    text,
                    line_start,
                    end,
                    line_end,
                    _config_key_start(text, line_start, label_start),
                )
            break
        pos = line_end + 1
    return end if end > value_start else None


#: A YAML mapping key or a list item opening a line: no continuation of a value.
_YAML_ENTRY_RE = re.compile(r"\A(?:[\w.\"'-]{1,128}[ \t]{0,64}:(?:[ \t]|\Z)|-(?:[ \t]|\Z)|#)")


def folded_plain_end(text: str, line_start: int, end: int, line_end: int, key_start: int) -> int:
    """End of a plain YAML value whose line ends at *line_end*, through the lines
    YAML folds into it: each one indented deeper than the key's line, not itself
    a key, a list item or a comment (``password: AbCd`` then ``  EfGh``). Blank
    lines between them fold too. *end* when no line follows it."""
    # The key's column, from the part of its line before the key only: never
    # the value or a later line (``note: "      text"``).
    head = text[line_start:key_start]
    prefix = LINE_PREFIX.get()
    if line_start == 0 and isinstance(prefix, str) and prefix is not _DECODED:
        head = prefix + head
    indent = key_indent(head)
    nxt = line_end
    while True:
        if nxt >= len(text):
            break
        start = nxt + 1
        stop = text.find("\n", start)
        stop = len(text) if stop == -1 else stop
        line = text[start:stop]
        body = line.lstrip(" ")
        if body.strip():
            if len(line) - len(body) <= indent or _YAML_ENTRY_RE.match(body) is not None:
                break
            comment = _CONFIG_COMMENT_RE.search(text, start, stop)
            end = start + len(text[start : comment.start() if comment else stop].rstrip())
            if comment is not None:
                break
        nxt = stop
    return end


#: A file path a label names (``/usr/local/bin``, ``./config.yaml``,
#: ``~/.ssh/id_rsa``): lower-case segments only. A base64 secret that merely
#: starts with ``/`` carries upper case too, so it is no path.
_PATH_VALUE_RE = re.compile(r"(?:~|\.{1,2})?(?:/[a-z0-9._-]+)+/?")


def labelled_value_is_secret(
    value: str,
    *,
    quoted: bool,
    next_char: str,
    aws: bool = False,
    weak: bool = False,
    config: bool = False,
    yaml: bool = False,
) -> bool:
    """Whether a value behind a secret label is a secret.

    It is, unless the whole value is exempt (:func:`is_exempt_value`), it can
    only name something (:func:`is_naming_value`), or, written unquoted, it is
    called or subscripted (*next_char* is ``(`` or ``[``: ``load_key(path)``,
    ``os.environ[...]``). Behind an AWS secret label (*aws*) a value shaped
    like a key ID is still the secret the label names, so it is not vetoed.
    A run of digits is never a name behind a secret label: it can be a PIN or
    a numeric password, and an account ID keeps its own label (``account``).
    """
    # Every secret label is strong on a config line (``DB_PASS=...``,
    # ``PWD=...``): the form says the value is the field's literal. Behind a
    # weak label a ``key: value`` line is also how logs and prose print a
    # status or a path (``first_pass: completed``, ``pwd: /usr/local``), so
    # there the weak word, length and path tests below still apply.
    if config and not quoted and not (weak and yaml):
        # On a config line (:func:`config_value_end`) behind a strong label the
        # value is a literal, always Secret: only an exact variable reference
        # names it instead. A key ID, account ID or ARN keeps its Identifier tier.
        if (
            not value
            or is_tag(value)
            or _DIGEST_MASK_RE.fullmatch(value) is not None
            or _CONFIG_REFERENCE_RE.fullmatch(value) is not None
            # A YAML null or boolean (``password: null``) is a typed value, no
            # string: the same word in dotenv (``PASSWORD=false``) is one.
            or (yaml and _YAML_NULL_OR_BOOL_RE.fullmatch(value) is not None)
        ):
            return False
        return aws or value.isdigit() or not is_naming_value(value)
    if quoted and weak and _UNICODE_ESCAPES_RE.fullmatch(value):
        # A literal written only as ``\\uXXXX`` escapes (``'\\u7eff\\u706f'``) is
        # judged by the characters it spells.
        value = value.encode("ascii").decode("unicode_escape")
    pin = len(value) >= MIN_PIN_LEN and value.isdigit()
    if (
        quoted
        and weak
        and not pin
        and not config
        and (_is_word(value) or (value.isalpha() and value.lower() == value.upper()))
    ):
        # A quoted value behind a weak label keeps ONLY the word test
        # (``{"pwd": "deny"}``, a word of a script with no case); no length,
        # path or dotted-name test applies, so a short password goes.
        return False
    if (
        not quoted
        and next_char in _TYPE_OR_NAME_FOLLOW
        and (_TYPE_NAME_RE.fullmatch(value) or _MINIFIED_NAME_RE.fullmatch(value))
    ):
        # A type in an annotation (``def login(password: str)``, ``password:
        # string;``) or a minified name (``{password:t}``) names no value: only
        # an exact type from a closed list, or one character, and only when code
        # punctuation, a blank or the line end follows it.
        return False
    if not quoted and next_char in (" ", "\t") and _OPERAND_KEYWORD_RE.fullmatch(value):
        # A keyword that takes an operand (``apiKey = await res.text()``,
        # ``password = new String(chars)``): the value is the expression after it.
        return False
    if is_exempt_value(value, aws=aws, quoted=quoted, weak=weak):
        return False
    if not aws and not value.isdigit() and is_naming_value(value):
        return False
    return quoted or next_char not in ("(", "[")
