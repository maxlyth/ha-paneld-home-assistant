"""Tests for pure deterministic first-install plan binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, replace
from unittest.mock import patch

import pytest

from custom_components.ha_paneld import install_plan
from custom_components.ha_paneld.client import PanelAddress
from custom_components.ha_paneld.install_jobs import InstallJobStoreError
from custom_components.ha_paneld.install_network import PinnedPanelTarget
from custom_components.ha_paneld.install_plan import (
    InstallPlanError,
    InstallPlanErrorCode,
    build_install_plan,
)
from custom_components.ha_paneld.provisioning import (
    InstallTargetProbe,
    InstallTargetState,
)
from custom_components.ha_paneld.release import InstallDescriptor, ReleaseArtifact

APK_SHA256 = "a" * 64
CREDENTIAL_ID = "b" * 64
SIGNER_SHA256 = "ac6193307fb0b70113aae205d7549406f96e063bc5491b67b1d5694a34b0e339"


def pinned_target() -> PinnedPanelTarget:
    return PinnedPanelTarget(
        original=PanelAddress(host="panel.local", port=8888),
        pinned=PanelAddress(host="192.168.1.23", port=8888),
    )


def probe(**changes: object) -> InstallTargetProbe:
    value = InstallTargetProbe(
        state=InstallTargetState.INSTALL_CANDIDATE,
        model="Test Pänel",
        serial="SERIAL-1",
        primary_abi="arm64-v8a",
        android_sdk=34,
    )
    return replace(value, **changes)


def descriptor(**changes: object) -> InstallDescriptor:
    value = InstallDescriptor(
        schema="io.github.maxlyth.hapaneld.install.v1",
        release_tag="v0.1.0",
        version_name="0.1.0",
        version_code=100,
        apk_name="ha-paneld-v0.1.0-manual-setup-required.apk",
        apk_size=12_345,
        apk_sha256=APK_SHA256,
        package_id="io.github.maxlyth.hapaneld",
        signer_certificate_sha256=SIGNER_SHA256,
        min_sdk=26,
        supported_abis=("arm64-v8a", "armeabi-v7a"),
        database_compatibility="hapaneld-db:v1:ha-paneld.db:1:14",
        launch_component="io.github.maxlyth.hapaneld/.MainActivity",
    )
    return replace(value, **changes)


def release(**changes: object) -> ReleaseArtifact:
    value = ReleaseArtifact(
        tag="v0.1.0",
        version="0.1.0",
        apk_name="ha-paneld-v0.1.0-manual-setup-required.apk",
        apk_url="https://github.invalid/secret-location.apk?token=do-not-persist",
        sha256=APK_SHA256,
        descriptor=descriptor(),
    )
    return replace(value, **changes)


def rc_release(tag: str = "v0.9.7-rc3") -> ReleaseArtifact:
    apk_name = f"ha-paneld-{tag}-manual-setup-required.apk"
    return release(
        tag=tag,
        version=tag[1:],
        apk_name=apk_name,
        descriptor=descriptor(release_tag=tag, version_name=tag[1:], apk_name=apk_name),
    )


def test_rc_plan_binds_only_explicit_exact_opt_in() -> None:
    plan = build_install_plan(
        pinned_target(),
        probe(),
        rc_release(),
        CREDENTIAL_ID,
        expected_rc_tag="v0.9.7-rc3",
    )
    assert plan.artifact.release_tag == "v0.9.7-rc3"
    assert plan.artifact.version_name == "0.9.7-rc3"
    assert plan.artifact.apk_name == "ha-paneld-v0.9.7-rc3-manual-setup-required.apk"
    stable = build_install_plan(pinned_target(), probe(), release(), CREDENTIAL_ID)
    assert plan.plan_sha256 != stable.plan_sha256
    assert asdict(plan.artifact).keys() == asdict(stable.artifact).keys()


@pytest.mark.parametrize(
    "requested", [None, "", "v0.9.7", "v0.9.7-rc4", "v0.9.7-rc03", True, 3]
)
def test_rc_plan_refuses_missing_malformed_or_different_opt_in(
    requested: object,
) -> None:
    with pytest.raises(InstallPlanError) as caught:
        build_install_plan(
            pinned_target(),
            probe(),
            rc_release(),
            CREDENTIAL_ID,
            expected_rc_tag=requested,  # type: ignore[arg-type]
        )
    assert caught.value.code is InstallPlanErrorCode.INVALID_RELEASE


def test_rc_plan_refuses_stable_substitution() -> None:
    with pytest.raises(InstallPlanError) as caught:
        build_install_plan(
            pinned_target(),
            probe(),
            release(),
            CREDENTIAL_ID,
            expected_rc_tag="v0.9.7-rc3",
        )
    assert caught.value.code is InstallPlanErrorCode.INVALID_RELEASE


def assert_error(
    code: InstallPlanErrorCode,
    *,
    target: object = None,
    target_probe: object = None,
    artifact: object = None,
    credential_id: object = CREDENTIAL_ID,
) -> None:
    with pytest.raises(InstallPlanError) as caught:
        build_install_plan(
            pinned_target() if target is None else target,  # type: ignore[arg-type]
            probe() if target_probe is None else target_probe,  # type: ignore[arg-type]
            release() if artifact is None else artifact,  # type: ignore[arg-type]
            credential_id,  # type: ignore[arg-type]
        )
    assert caught.value.code is code
    assert str(caught.value) == code.value


def test_build_maps_every_field_and_hashes_exact_canonical_plan() -> None:
    plan = build_install_plan(pinned_target(), probe(), release(), CREDENTIAL_ID)

    assert asdict(plan.target) == {
        "address": "panel.local",
        "pinned_address": "192.168.1.23",
        "adb_serial": "SERIAL-1",
        "model": "Test Pänel",
        "primary_abi": "arm64-v8a",
        "android_sdk": 34,
    }
    assert asdict(plan.artifact) == {
        "descriptor_schema": "io.github.maxlyth.hapaneld.install.v1",
        "release_tag": "v0.1.0",
        "version_name": "0.1.0",
        "version_code": 100,
        "apk_name": "ha-paneld-v0.1.0-manual-setup-required.apk",
        "apk_sha256": APK_SHA256,
        "apk_size": 12_345,
        "package_id": "io.github.maxlyth.hapaneld",
        "signer_certificate_sha256": SIGNER_SHA256,
        "min_sdk": 26,
        "supported_abis": ("arm64-v8a", "armeabi-v7a"),
        "database_compatibility": "hapaneld-db:v1:ha-paneld.db:1:14",
        "launch_component": "io.github.maxlyth.hapaneld/.MainActivity",
    }
    assert plan.adb_credential_id == CREDENTIAL_ID
    canonical = (
        json.dumps(
            {
                "schema": "io.github.maxlyth.hapaneld.install-plan.v1",
                "target": asdict(plan.target),
                "artifact": asdict(plan.artifact),
                "adb_credential_id": CREDENTIAL_ID,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    assert plan.plan_sha256 == hashlib.sha256(canonical).hexdigest()
    assert canonical.endswith(b"\n")
    assert canonical.count(b"\n") == 1
    assert b"github.invalid" not in canonical
    assert b"do-not-persist" not in canonical

    with pytest.raises(FrozenInstanceError):
        plan.plan_sha256 = "0" * 64  # type: ignore[misc]


def test_hash_is_deterministic_and_release_url_is_irrelevant() -> None:
    first = build_install_plan(pinned_target(), probe(), release(), CREDENTIAL_ID)
    second = build_install_plan(
        pinned_target(),
        probe(),
        release(apk_url="not-even-a-url"),
        CREDENTIAL_ID,
    )

    assert first == second


def test_plan_delegates_exact_validated_inputs_to_job_digest_authority() -> None:
    with patch.object(
        install_plan,
        "install_plan_sha256",
        wraps=install_plan.install_plan_sha256,
    ) as digest:
        plan = build_install_plan(pinned_target(), probe(), release(), CREDENTIAL_ID)

    digest.assert_called_once_with(plan.target, plan.artifact, CREDENTIAL_ID)


def test_job_digest_failure_is_mapped_without_detail() -> None:
    with (
        patch.object(
            install_plan,
            "install_plan_sha256",
            side_effect=InstallJobStoreError("private implementation detail"),
        ),
        pytest.raises(InstallPlanError) as caught,
    ):
        build_install_plan(pinned_target(), probe(), release(), CREDENTIAL_ID)

    assert caught.value.code is InstallPlanErrorCode.INVALID_PLAN
    assert str(caught.value) == "invalid_plan"


@pytest.mark.parametrize(
    "target",
    [
        object(),
        PinnedPanelTarget(
            original=PanelAddress("panel.local", 8888),
            pinned=PanelAddress("192.168.1.23", 5555),
        ),
        PinnedPanelTarget(
            original=PanelAddress("192.168.1.24", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("bad host", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("panel.local", 8888),
            pinned=PanelAddress("203.0.113.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("panel.local", 8888),
            pinned=PanelAddress("not-an-ip", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("Panel.local", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=object(),  # type: ignore[arg-type]
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("a" * 256, 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("täst.local", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("bad_host.local", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("192.168.1", 8888),
            pinned=PanelAddress("192.168.1.23", 8888),
        ),
        PinnedPanelTarget(
            original=PanelAddress("panel.local", True),  # type: ignore[arg-type]
            pinned=PanelAddress("192.168.1.23", True),  # type: ignore[arg-type]
        ),
    ],
)
def test_rejects_malformed_pinned_target(target: object) -> None:
    assert_error(InstallPlanErrorCode.INVALID_TARGET, target=target)


def test_rejects_non_candidate_and_non_probe() -> None:
    assert_error(
        InstallPlanErrorCode.PROBE_NOT_INSTALL_CANDIDATE,
        target_probe=probe(state=InstallTargetState.INSTALLED),
    )
    assert_error(InstallPlanErrorCode.INCOMPLETE_PROBE, target_probe=object())


@pytest.mark.parametrize(
    "changes",
    [
        {"model": None},
        {"model": " leading"},
        {"model": "x\x00y"},
        {"serial": None},
        {"serial": "bad serial"},
        {"primary_abi": None},
        {"primary_abi": "bad/abi"},
        {"android_sdk": None},
        {"android_sdk": True},
        {"android_sdk": 101},
    ],
)
def test_rejects_incomplete_or_malformed_probe(changes: dict[str, object]) -> None:
    assert_error(
        InstallPlanErrorCode.INCOMPLETE_PROBE,
        target_probe=probe(**changes),
    )


def test_rejects_legacy_preview_only_release() -> None:
    assert_error(
        InstallPlanErrorCode.PREVIEW_ONLY_RELEASE,
        artifact=release(descriptor=None),
    )


def test_rejects_malformed_descriptor_object() -> None:
    assert_error(
        InstallPlanErrorCode.INVALID_RELEASE,
        artifact=release(descriptor=object()),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "future-schema"),
        ("release_tag", "v01.0.0"),
        ("version_name", "0.1.1"),
        ("version_code", True),
        ("version_code", 2**31),
        ("apk_name", "other.apk"),
        ("apk_size", 0),
        ("apk_size", 64 * 1024 * 1024 + 1),
        ("apk_sha256", "A" * 64),
        ("package_id", "other.package"),
        ("signer_certificate_sha256", "c" * 64),
        ("min_sdk", True),
        ("min_sdk", 101),
        ("supported_abis", ("arm64-v8a",)),
        ("database_compatibility", "hapaneld-db:v1:ha-paneld.db:2:1"),
        ("database_compatibility", "hapaneld-db:v1:ha-paneld.db:1:99999999999"),
        ("launch_component", "other/.MainActivity"),
    ],
)
def test_rejects_malformed_descriptor(field: str, value: object) -> None:
    assert_error(
        InstallPlanErrorCode.INVALID_RELEASE,
        artifact=release(descriptor=descriptor(**{field: value})),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag", "v0.1.1"),
        ("version", "0.1.1"),
        ("apk_name", "different.apk"),
        ("sha256", "c" * 64),
    ],
)
def test_rejects_release_metadata_not_bound_to_descriptor(
    field: str, value: object
) -> None:
    assert_error(
        InstallPlanErrorCode.INVALID_RELEASE,
        artifact=release(**{field: value}),
    )


def test_rejects_wrong_release_type_without_leaking_it() -> None:
    secret = object()
    assert_error(InstallPlanErrorCode.INVALID_RELEASE, artifact=secret)


@pytest.mark.parametrize("credential_id", ["A" * 64, "b" * 63, None, True])
def test_rejects_noncanonical_credential_id(credential_id: object) -> None:
    assert_error(
        InstallPlanErrorCode.INVALID_CREDENTIAL,
        credential_id=credential_id,
    )


def test_rejects_release_incompatible_with_exact_probe() -> None:
    assert_error(
        InstallPlanErrorCode.INCOMPATIBLE_RELEASE,
        artifact=release(descriptor=descriptor(min_sdk=35)),
    )
    assert_error(
        InstallPlanErrorCode.INCOMPATIBLE_RELEASE,
        target_probe=probe(primary_abi="x86"),
    )


def test_nondefault_port_and_ipv6_identity_are_preserved() -> None:
    target = PinnedPanelTarget(
        original=PanelAddress("panel.local", 8889),
        pinned=PanelAddress("fd00::23", 8889),
    )

    plan = build_install_plan(target, probe(), release(), CREDENTIAL_ID)

    assert plan.target.address == "panel.local:8889"
    assert plan.target.pinned_address == "[fd00::23]:8889"


def test_safe_original_ip_identity_is_preserved() -> None:
    target = PinnedPanelTarget(
        original=PanelAddress("192.168.1.23", 8888),
        pinned=PanelAddress("192.168.1.23", 8888),
    )

    plan = build_install_plan(target, probe(), release(), CREDENTIAL_ID)

    assert plan.target.address == "192.168.1.23"
    assert plan.target.pinned_address == "192.168.1.23"
