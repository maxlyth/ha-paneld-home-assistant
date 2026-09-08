"""Discovery includes only complete, supported release choices."""

import json

import pytest

from custom_components.panel_assistant import release_catalog as catalog
from custom_components.panel_assistant.release import ReleaseResolutionError

from .test_release import _FakeResponse, _FakeSession


def document(tag="v1.2.3", *, prerelease=False):
    apk = f"ha-paneld-{tag}-manual-setup-required.apk"
    descriptor = f"ha-paneld-{tag}-install.json"
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "body": "Untrusted release prose",
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"https://github.com/maxlyth/ha-paneld/releases/download/{tag}/{name}"
                ),
            }
            for name in [
                apk,
                apk + ".sha256",
                apk + ".sha256.sig",
                descriptor,
                descriptor + ".sig",
            ]
        ],
    }


def session(latest, recent):
    return _FakeSession(
        {
            str(url): _FakeResponse(200, json.dumps(value).encode(), url)
            for url, value in [
                (catalog._LATEST_RELEASE_URL, latest),
                (catalog._RECENT_RELEASES_URL, recent),
            ]
        }
    )


async def test_latest_stable_and_exact_recent_rc_only():
    rc = document("v1.3.0-rc2", prerelease=True)
    client = session(document(), [rc, document("v1.0.0"), rc])
    assert await catalog.async_list_install_releases(client) == [
        {"tag": "v1.2.3", "prerelease": False},
        {"tag": "v1.3.0-rc2", "prerelease": True},
    ]
    assert len(client.requests) == 2
    for _, options in client.requests:
        assert options["allow_redirects"] is False
        assert "Authorization" not in options["headers"]
        assert options["timeout"].total == 10


async def test_total_choice_limit_includes_latest_stable():
    recent = [document(f"v1.3.0-rc{n}", prerelease=True) for n in range(30, 0, -1)]
    choices = await catalog.async_list_install_releases(session(document(), recent))
    assert len(choices) == 30
    assert choices[0] == {"tag": "v1.2.3", "prerelease": False}
    assert choices[-1] == {"tag": "v1.3.0-rc2", "prerelease": True}


@pytest.mark.parametrize("missing", range(5))
async def test_every_install_asset_is_required(missing):
    latest = document()
    latest["assets"].pop(missing)
    rc = document("v1.3.0-rc2", prerelease=True)
    assert await catalog.async_list_install_releases(session(latest, [rc])) == [
        {"tag": "v1.3.0-rc2", "prerelease": True}
    ]


async def test_latest_cannot_substitute_rc_and_bad_rows_are_ignored():
    rc = document("v1.3.0-rc2", prerelease=True)
    assert await catalog.async_list_install_releases(session(rc, [None, rc])) == [
        {"tag": "v1.3.0-rc2", "prerelease": True}
    ]


@pytest.mark.parametrize(
    "change", ["missing", "url", "duplicate", "draft", "flag", "tag"]
)
async def test_bad_choices_are_omitted(change):
    candidate = document("v1.3.0-rc2", prerelease=True)
    if change == "missing":
        candidate["assets"] = candidate["assets"][:3]
    elif change == "url":
        candidate["assets"][0]["browser_download_url"] = "https://example.com/a.apk"
    elif change == "duplicate":
        candidate["assets"].append(candidate["assets"][0])
    elif change == "draft":
        candidate["draft"] = True
    elif change == "flag":
        candidate["prerelease"] = False
    else:
        candidate["tag_name"] = "v1.3.0-beta1"
    latest = document()
    latest["assets"] = []
    assert await catalog.async_list_install_releases(session(latest, [candidate])) == []


@pytest.mark.parametrize("recent", [{}, [None] * 31])
async def test_invalid_catalog_shape_or_count_fails(recent):
    with pytest.raises(ReleaseResolutionError):
        await catalog.async_list_install_releases(session(document(), recent))


@pytest.mark.parametrize(
    "body,status",
    [
        (b"x" * (catalog._MAX_CATALOG_BYTES + 1), 200),
        (b"[]", 403),
        (b"[]", 302),
        (b'{"duplicate":1,"duplicate":2}', 200),
        (b"[NaN]", 200),
        (b"\xff", 200),
    ],
)
async def test_bounded_and_unambiguous_response(body, status):
    client = session(document(), [])
    client._responses[str(catalog._RECENT_RELEASES_URL)] = _FakeResponse(
        status, body, catalog._RECENT_RELEASES_URL
    )
    with pytest.raises(ReleaseResolutionError):
        await catalog.async_list_install_releases(client)
