"""talk2view.skill_format: the Agent Skills rules (agentskills.io/specification) for the
skills the SDK writes, and a frontmatter reader that keeps what it does not change."""

import pytest

from talk2view.skill_format import (
    LONG_BODY_LINES,
    Problem,
    body_is_long,
    from_form,
    parse,
    render,
    suggest_name,
    to_skill,
    validate,
    with_fields,
)

SPEC_EXAMPLE = """---
name: pdf-processing
description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
license: Apache-2.0
metadata:
  author: example-org
  version: "1.0"
---
# PDF

Use pdfplumber.
"""


def fields(problems):
    return [p.field for p in problems]


# ── names: the spec's own valid and invalid examples ──

@pytest.mark.parametrize("name", ["pdf-processing", "data-analysis", "code-review", "a", "x" * 64])
def test_valid_names(name):
    doc = from_form(name, "Does a thing. Use when asked.", "Body.")
    assert "name" not in fields(validate(doc))


@pytest.mark.parametrize(
    "name",
    ["PDF-Processing", "-pdf", "pdf-", "pdf--processing", "", "x" * 65, "my_skill", "claude-tools",
     "anthropic-helper", "a<b>c"],
)
def test_invalid_names(name):
    doc = from_form(name, "Does a thing. Use when asked.", "Body.")
    assert "name" in fields(validate(doc))


def test_name_must_match_folder():
    doc = from_form("pdf-processing", "Does a thing.", "Body.")
    assert fields(validate(doc, folder="pdf")) == ["name"]
    assert validate(doc, folder="pdf-processing") == []


# ── description, optional fields, body ──

@pytest.mark.parametrize("description", ["", "x" * 1025, "Uses <b>bold</b> tags"])
def test_invalid_descriptions(description):
    assert "description" in fields(validate(from_form("ok-name", description, "Body.")))


def test_description_at_the_limit_is_fine():
    assert validate(from_form("ok-name", "x" * 1024, "Body.")) == []


def test_empty_body_is_a_problem():
    assert fields(validate(from_form("ok-name", "Does it.", "   "))) == ["body"]


def test_long_body_is_not_a_problem():
    assert validate(from_form("ok-name", "Does it.", "line\n" * 800)) == []


def test_compatibility_over_500_is_a_problem():
    text = "---\nname: a\ndescription: d\ncompatibility: " + "x" * 501 + "\n---\nB\n"
    doc, problems = parse(text)
    assert problems == []
    assert fields(validate(doc)) == ["compatibility"]


# ── parsing ──

def test_parse_spec_example():
    doc, problems = parse(SPEC_EXAMPLE)
    assert problems == []
    assert doc.name == "pdf-processing"
    assert doc.description.startswith("Extract PDF text")
    assert doc.body == "# PDF\n\nUse pdfplumber.\n"
    assert [k for k, _ in doc.blocks] == ["name", "description", "license", "metadata"]
    assert validate(doc, folder="pdf-processing") == []


def test_parse_quoted_and_block_scalars():
    text = (
        "---\n"
        "name: 'quoted-name'\n"
        'description: "Says \\"hi\\". Use when greeting."\n'
        "compatibility: >\n  Needs Slicer\n  5.12 or newer\n"
        "---\nBody\n"
    )
    doc, problems = parse(text)
    assert problems == []
    assert doc.name == "quoted-name"
    assert doc.description == 'Says "hi". Use when greeting.'
    assert dict(doc.blocks)["compatibility"].startswith("compatibility: >")


def test_parse_allowed_tools_space_separated():
    doc, _ = parse("---\nname: a\ndescription: d\nallowed-tools: Read Bash(git:*)\n---\nB\n")
    assert doc.allowed_tools == ["Read", "Bash(git:*)"]


def test_no_frontmatter_uses_fallback_name():
    doc, problems = parse("# Just instructions\n", fallback_name="notes")
    assert doc.name == "notes" and doc.description == "" and doc.body == "# Just instructions\n"
    assert problems == []


def test_unsupported_yaml_is_a_problem_on_its_field():
    doc, problems = parse("---\nname: a\ndescription: d\nmetadata:\n  - one\n---\nB\n")
    assert fields(problems) == ["metadata"]


def test_metadata_values_must_be_strings_of_one_line():
    _doc, problems = parse("---\nname: a\ndescription: d\nmetadata:\n  a:\n    b: c\n---\nB\n")
    assert fields(problems) == ["metadata"]


def test_metadata_must_be_a_map_not_a_scalar():
    text = "---\nname: a\ndescription: d\nmetadata: just-a-scalar-not-a-map\n---\nB\n"
    doc, problems = parse(text)
    assert fields(problems) == ["metadata"]
    # validate() re-checks the raw block itself, so it catches this even for a doc that
    # did not come from parse() (e.g. one hand-built with with_fields).
    assert fields(validate(doc)) == ["metadata"]


def test_metadata_with_no_lines_is_a_problem():
    text = "---\nname: a\ndescription: d\nmetadata:\n---\nB\n"
    doc, problems = parse(text)
    assert fields(problems) == ["metadata"]
    assert fields(validate(doc)) == ["metadata"]


def test_metadata_map_is_not_a_problem():
    doc, problems = parse(SPEC_EXAMPLE)
    assert problems == []
    assert validate(doc, folder="pdf-processing") == []


# ── rendering and editing ──

def test_render_round_trips_byte_for_byte():
    doc, _ = parse(SPEC_EXAMPLE)
    assert render(doc) == SPEC_EXAMPLE


def test_with_fields_keeps_other_blocks_byte_for_byte():
    doc, _ = parse(SPEC_EXAMPLE)
    edited = with_fields(doc, description="Handles PDFs. Use for PDF work.", body="New body.\n")
    text = render(edited)
    assert "license: Apache-2.0\nmetadata:\n  author: example-org\n  version: \"1.0\"\n" in text
    assert "description: Handles PDFs. Use for PDF work.\n" in text
    assert text.endswith("---\nNew body.\n")
    assert parse(text)[0].description == "Handles PDFs. Use for PDF work."


def test_render_quotes_values_that_need_it():
    doc = from_form("a-b", "Colons: here # and a hash", "B")
    reparsed, problems = parse(render(doc))
    assert problems == [] and reparsed.description == "Colons: here # and a hash"


def test_render_multiline_description_survives():
    doc = from_form("a-b", "Line one.\nLine two.", "B")
    assert parse(render(doc))[0].description == "Line one.\nLine two."


def test_from_form_puts_name_then_description():
    text = render(from_form("a-b", "Does it.", "Body"))
    assert text == "---\nname: a-b\ndescription: Does it.\n---\nBody\n"


# ── names from free text ──

@pytest.mark.parametrize(
    "text, name",
    [
        ("Report volumes in mL", "report-volumes-in-ml"),
        ("  CT -- window__presets!! ", "ct-window-presets"),
        ("Claude helper", "helper"),
        ("Ärzte Übersicht", "arzte-ubersicht"),
        ("", "skill"),
        ("x" * 80, "x" * 64),
    ],
)
def test_suggest_name(text, name):
    assert suggest_name(text) == name
    assert "name" not in fields(validate(from_form(suggest_name(text), "Does it.", "B")))


def test_to_skill_matches_the_engine_payload():
    skill = to_skill(parse(SPEC_EXAMPLE)[0])
    assert (skill.name, skill.content) == ("pdf-processing", "# PDF\n\nUse pdfplumber.")
    assert skill.allowed_tools is None


def test_problem_is_a_value():
    assert Problem("name", "x") == Problem("name", "x")


# ── body_is_long: a hint, not a validation failure (see test_long_body_is_not_a_problem) ──

def test_body_is_long_at_the_threshold_is_not_long():
    doc = from_form("ok-name", "Does it.", "\n".join(["line"] * LONG_BODY_LINES))
    assert body_is_long(doc) is False


def test_body_is_long_past_the_threshold():
    doc = from_form("ok-name", "Does it.", "\n".join(["line"] * (LONG_BODY_LINES + 1)))
    assert body_is_long(doc) is True


# ── What render() writes, parse() reads back exactly ─────────────────────────────────

#: Every character str.splitlines() breaks at other than "\n".
ODD_LINE_BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("brk", ODD_LINE_BREAKS)
def test_an_odd_line_break_in_the_description_is_refused(brk):
    problems = validate(from_form("ok-name", "Does a" + brk + "b. Use when c.", "B"))
    assert [(p.field, p.message) for p in problems] == [
        (
            "description",
            "contains a line-break character that isn't a normal newline "
            "(e.g. from a pasted document); remove it",
        )
    ]


@pytest.mark.parametrize("brk", ODD_LINE_BREAKS)
def test_an_odd_line_break_in_the_name_is_refused_with_the_same_words(brk):
    problems = validate(from_form("ok" + brk + "name", "Does it.", "B"))
    assert [p.field for p in problems] == ["name"]
    assert "isn't a normal newline" in problems[0].message


def _round_trip_corpus():
    import random

    pieces = [
        "Does x.", "Use when y", '"quoted"', "'single'", "a: b", "key:", " #not a comment",
        "\t#tab hash", "\ttab", "tab\t", " lead", "trail ", "\n", "\n\n", "  indented",
        "ünïcödé", "日本語", "emoji 🎉", "\\back\\slash", "\\n literal", "- dash", "* star",
        "& amp", "! bang", "| pipe", "> gt", "% pct", "@ at", "` tick", "{brace}", "[list]",
        ",comma", "?q", "#hash", "---", "...", "\xa0nbsp", "x\xa0", ":colon", "a #b",
    ]
    rng = random.Random(20260929)  # noqa: S311 - a fixed corpus, not a secret
    corpus = list(pieces)
    for _ in range(3000):
        corpus.append("".join(rng.choice(pieces) for _ in range(rng.randint(1, 6))))
    return [c for c in corpus if c.strip()]


def test_render_then_parse_gives_back_every_description():
    """A property test over thousands of descriptions (quotes, colons, " #", tabs,
    newlines -- leading indentation and trailing newlines included -- unicode): whatever
    validates is written so that parse() reads back exactly that description."""
    failures = []
    for description in _round_trip_corpus():
        doc = from_form("ok-name", description, "Body.")
        if validate(doc):
            continue
        back, problems = parse(render(doc))
        if problems or back.description != description:
            failures.append((description, back.description, problems))
    assert failures == []


def test_multi_line_descriptions_keep_their_indentation_and_trailing_newline():
    for description in ("  both lines\n  indented", "ends with a newline\n", "a\n   \nb"):
        back, problems = parse(render(from_form("ok-name", description, "Body.")))
        assert problems == [] and back.description == description


def test_an_ordinary_multi_line_description_is_still_a_block_literal():
    text = render(from_form("ok-name", "Line one.\n  Indented two.", "Body."))
    assert "description: |-\n  Line one.\n    Indented two.\n" in text
