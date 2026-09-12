"""Check that every help topic Panel Assistant links to is known to the site.

Panel Assistant links outward only through https://panel-assistant.io/go/<topic>.
The site answers an unknown topic with a redirect to its homepage that carries
``go=<topic>``, so a misspelt or unregistered topic never looks broken: it just
quietly lands a stuck user somewhere unhelpful. This finds every topic the
integration ships and asks the live site about each one.

The tests import ``topics_in_repository`` to check the same topics offline
against a pinned list; run as a script, it performs the live check.
"""

from __future__ import annotations

import ast
import http.client
import re
import sys
from pathlib import Path

SITE_HOST = "panel-assistant.io"
# Hostnames are case-insensitive, so an uppercase host is still a link to check.
_GO_BASE = re.compile(r"panel-assistant\.io/go/", re.IGNORECASE)
_TOPIC = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# What may follow a topic in a link: the end of the text, a query or anchor, or
# the character that closes the string, attribute or Markdown link around it.
_TOPIC_END = set("?#\"'`)]<> \t\r\n")
# Punctuation that ends a sentence, as opposed to code continuing the URL.
_PROSE_END = set(".,;:!")
# Where shipped links can live. Python is scanned as text as well as for
# help_url calls, so a URL written out by hand cannot slip past. The built
# bundle is scanned too; a topic found twice is still one topic.
_LINK_SUFFIXES = {".md", ".json", ".mjs", ".js", ".html", ".py"}
_SKIP_PARTS = {"node_modules", "dist", ".git"}
# The one place a bare base URL is legitimate: help_url's own definition, which
# appends the topic. Anywhere else a bare base means a topic assembled at run
# time, which cannot be verified.
_HELP_REDIRECT_DEFINITION = '_HELP_REDIRECT = "https://panel-assistant.io/go/"'


class TopicError(ValueError):
    """A help link whose topic cannot be verified."""


def topics_in_python(source: str, filename: str = "<source>") -> set[str]:
    """Return every topic passed to ``help_url`` in one Python source file.

    A topic that is not a string literal cannot be checked against the site, so
    it is refused rather than skipped: skipping it is how an unverified link ships.
    """
    topics: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "help_url":
            continue
        if not node.args:
            raise TopicError(f"{filename}:{node.lineno}: help_url() without a topic")
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            raise TopicError(
                f"{filename}:{node.lineno}: help_url() topic must be a string literal"
            )
        if not _TOPIC.fullmatch(first.value):
            raise TopicError(
                f"{filename}:{node.lineno}: malformed topic {first.value!r}"
            )
        topics.add(first.value)
    return topics


def topics_in_text(text: str, filename: str = "<text>") -> set[str]:
    """Return every topic in a panel-assistant.io/go/ link in some text.

    A link whose topic is not written out in full, such as a template literal
    or a string that is concatenated later, is refused for the same reason a
    non-literal help_url topic is: it cannot be checked against the site.
    """
    topics: set[str] = set()
    for match in _GO_BASE.finditer(text):
        rest = text[match.end() :]
        line = text.count("\n", 0, match.start()) + 1
        topic = _TOPIC.match(rest)
        if topic is None:
            raise TopicError(
                f"{filename}:{line}: a /go/ link whose topic is not written out "
                "in full cannot be checked against the site"
            )
        following = rest[topic.end() : topic.end() + 1]
        if following and following not in _TOPIC_END:
            if following in _PROSE_END:
                # A URL ending a sentence: the topic is whole, so name the stray
                # character rather than sending someone to look at the topic.
                raise TopicError(
                    f"{filename}:{line}: the /go/ topic {topic.group()!r} is followed "
                    f"by {following!r}, which cannot end a link; in prose, wrap the "
                    "URL as a Markdown link"
                )
            # Anything else is code continuing the URL, such as a placeholder or
            # a template literal, and the matched prefix is not the real topic.
            raise TopicError(
                f"{filename}:{line}: a /go/ link whose topic is not written out "
                f"in full cannot be checked against the site (it continues with "
                f"{following!r})"
            )
        topics.add(topic.group())
    return topics


def topics_in_repository(root: Path) -> set[str]:
    """Return every help topic the repository ships, from code and documents."""
    topics: set[str] = set()
    for path in sorted((root / "custom_components").rglob("*.py")):
        if _SKIP_PARTS.isdisjoint(path.parts):
            topics |= topics_in_python(path.read_text(encoding="utf-8"), str(path))
    candidates = [root / "README.md"]
    candidates += sorted((root / "custom_components").rglob("*"))
    for path in candidates:
        if (
            path.is_file()
            and path.suffix in _LINK_SUFFIXES
            and _SKIP_PARTS.isdisjoint(path.parts)
        ):
            text = path.read_text(encoding="utf-8")
            if path.name == "const.py":
                text = text.replace(_HELP_REDIRECT_DEFINITION, "", 1)
            topics |= topics_in_text(text, str(path))
    return topics


def classify(status: int, location: str | None) -> str:
    """Return "ok", "fallback" or "broken" for one live redirect."""
    if status not in (301, 302, 307, 308) or not location:
        return "broken"
    if "go=" in location:
        return "fallback"
    return "ok"


def _probe(topic: str) -> tuple[int, str | None]:
    connection = http.client.HTTPSConnection(SITE_HOST, timeout=20)
    try:
        # http.client never follows redirects, which is the point: the answer
        # being judged is the redirect itself, not the page it leads to.
        connection.request(
            "GET",
            f"/go/{topic}",
            headers={"User-Agent": "panel-assistant-help-topic-check"},
        )
        response = connection.getresponse()
        location = response.getheader("Location")
        response.read()
        return response.status, location
    finally:
        connection.close()


# The integration requires this Python (pyproject.toml), and parsing its source
# needs the same grammar: 3.14 accepts an unparenthesised except list that
# older parsers reject as a SyntaxError.
_MINIMUM_PYTHON = (3, 14)


def main() -> int:
    if sys.version_info < _MINIMUM_PYTHON:
        print(
            f"Python {'.'.join(map(str, _MINIMUM_PYTHON))} or newer is required to "
            f"parse the integration's source; this is {sys.version.split()[0]}."
        )
        return 2
    root = Path(__file__).resolve().parents[2]
    topics = topics_in_repository(root)
    if not topics:
        print("No help topics found; refusing to report an empty check as a pass.")
        return 1
    failed = False
    for topic in sorted(topics):
        status, location = _probe(topic)
        verdict = classify(status, location)
        print(f"{verdict:8} {topic}: {status} {location or ''}")
        failed |= verdict != "ok"
    if failed:
        print(
            "\nA topic Panel Assistant links to is not known to the site. Add it to "
            "worker/topics.json in panel-assistant.io; links in a release that has "
            "already shipped start working as soon as the site deploys.",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
