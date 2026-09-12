"""Every help topic Panel Assistant links to must be one the site knows.

The site sends an unknown topic to its homepage rather than failing, so a bad
topic is invisible in use. This checks the shipped topics offline against a
pinned copy of the site's topic names; the release workflow asks the live site.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_help_topics", ROOT / ".github/scripts/check_help_topics.py"
)
assert _SPEC and _SPEC.loader
check_help_topics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_help_topics)

PINNED = set(
    json.loads((ROOT / "tests/fixtures/go_topics.json").read_text(encoding="utf-8"))[
        "topics"
    ]
)


def test_every_shipped_topic_is_known_to_the_site() -> None:
    """Add a new topic to the site's table first, then to the pinned list here."""
    unknown = check_help_topics.topics_in_repository(ROOT) - PINNED
    assert not unknown, f"topics the site does not know: {sorted(unknown)}"


def test_the_repository_scan_finds_the_topics_it_should() -> None:
    """Without this, a scan that found nothing would pass the check above."""
    found = check_help_topics.topics_in_repository(ROOT)
    assert {"panel-access", "panel-unreachable", "discord"} <= found


def test_a_literal_topic_is_extracted() -> None:
    source = 'x = help_url("panel-access")\ny = const.help_url("discord", model="a")\n'
    assert check_help_topics.topics_in_python(source) == {"panel-access", "discord"}


def test_an_unrelated_call_is_ignored() -> None:
    assert check_help_topics.topics_in_python('other_url("panel-access")\n') == set()


@pytest.mark.parametrize(
    "source",
    [
        "help_url(topic)\n",
        'help_url(f"panel-{kind}")\n',
        "help_url()\n",
        'help_url("Panel Access")\n',
    ],
)
def test_a_topic_that_cannot_be_verified_is_refused(source: str) -> None:
    """Skipping an unverifiable topic is exactly how an unchecked link ships."""
    with pytest.raises(check_help_topics.TopicError):
        check_help_topics.topics_in_python(source)


@pytest.mark.parametrize(
    "text",
    [
        "`https://panel-assistant.io/go/${topic}`",
        'url = "https://panel-assistant.io/go/" + topic',
        "https://panel-assistant.io/go/<topic>",
        "https://panel-assistant.io/go/Panel-Access",
        "https://panel-assistant.io/go/panel${suffix}",
    ],
)
def test_a_link_whose_topic_is_not_written_out_is_refused(text: str) -> None:
    """A template literal or a concatenated URL would otherwise pass unseen."""
    with pytest.raises(check_help_topics.TopicError):
        check_help_topics.topics_in_text(text)


def test_a_url_written_out_in_python_is_scanned(tmp_path: Path) -> None:
    """Python is scanned as text, not only for help_url calls."""
    package = tmp_path / "custom_components" / "panel_assistant"
    package.mkdir(parents=True)
    (package / "somewhere.py").write_text(
        'LINK = "https://panel-assistant.io/go/not-a-real-topic"\n', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    found = check_help_topics.topics_in_repository(tmp_path)
    assert found == {"not-a-real-topic"}


def test_only_help_urls_own_definition_may_hold_a_bare_base(tmp_path: Path) -> None:
    """The exemption is that one line; the same base anywhere else is refused."""
    package = tmp_path / "custom_components" / "panel_assistant"
    package.mkdir(parents=True)
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (package / "const.py").write_text(
        '_HELP_REDIRECT = "https://panel-assistant.io/go/"\n', encoding="utf-8"
    )
    assert check_help_topics.topics_in_repository(tmp_path) == set()
    # The identical definition line, copied into another file, is not exempt.
    (package / "other.py").write_text(
        '_HELP_REDIRECT = "https://panel-assistant.io/go/"\n', encoding="utf-8"
    )
    with pytest.raises(check_help_topics.TopicError):
        check_help_topics.topics_in_repository(tmp_path)


def test_a_bad_follower_is_named_rather_than_blamed_on_the_topic() -> None:
    """A trailing full stop is a prose problem, and the error has to say so."""
    with pytest.raises(check_help_topics.TopicError) as raised:
        check_help_topics.topics_in_text("See https://panel-assistant.io/go/discord.")
    assert "'discord'" in str(raised.value)
    assert "'.'" in str(raised.value)
    assert "not written out in full" not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "`https://panel-assistant.io/go/panel${suffix}`",
        'f"https://panel-assistant.io/go/panel-{kind}"',
        "https://panel-assistant.io/go/panel-Access",
        "https://panel-assistant.io/go/panel_access",
    ],
)
def test_code_continuing_a_url_is_not_given_prose_advice(text: str) -> None:
    """Only sentence punctuation gets the Markdown advice; code gets the real cause."""
    with pytest.raises(check_help_topics.TopicError) as raised:
        check_help_topics.topics_in_text(text)
    assert "not written out in full" in str(raised.value)
    assert "Markdown" not in str(raised.value)


def test_an_uppercase_host_is_still_checked() -> None:
    """Hostnames are case-insensitive, so this is a real link and must be found."""
    text = "https://Panel-Assistant.IO/go/panel-access"
    assert check_help_topics.topics_in_text(text) == {"panel-access"}


def test_go_links_in_documents_are_extracted() -> None:
    text = (
        "[a](https://panel-assistant.io/go/discord) and "
        "[b](https://panel-assistant.io/go/panel-access?v=1)"
    )
    assert check_help_topics.topics_in_text(text) == {"discord", "panel-access"}


def test_a_redirect_to_the_topic_is_ok() -> None:
    location = "https://panel-assistant.io/install/prepare-a-panel/?v=1#anchor"
    assert check_help_topics.classify(302, location) == "ok"


def test_the_homepage_fallback_is_caught() -> None:
    """The site marks an unknown topic with go=; that must never count as ok."""
    location = "https://panel-assistant.io/?v=1&go=panel-acess"
    assert check_help_topics.classify(302, location) == "fallback"


@pytest.mark.parametrize(
    ("status", "location"), [(200, None), (404, None), (302, None)]
)
def test_anything_but_a_redirect_is_broken(status: int, location: str | None) -> None:
    assert check_help_topics.classify(status, location) == "broken"
