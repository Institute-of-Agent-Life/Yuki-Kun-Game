"""Host versions follow the implemented SDK contract without a release ceiling."""

import tomllib
from pathlib import Path

import pytest
from yuki.plugin_host.manifest import ManifestValidationError, PluginManifest


@pytest.mark.parametrize("version", ["3.9.0", "4.0.0"])
def test_manifest_accepts_hosts_providing_its_sdk_contract(version):
    source = Path(__file__).parents[1] / "plugin.toml"
    manifest = PluginManifest.model_validate(tomllib.loads(source.read_text(encoding="utf-8")))
    manifest.check_compatibility(yuki_version=version)
    assert manifest.plugin_api == "7.0"


def test_manifest_keeps_the_minimum_host_version():
    source = Path(__file__).parents[1] / "plugin.toml"
    manifest = PluginManifest.model_validate(tomllib.loads(source.read_text(encoding="utf-8")))
    with pytest.raises(ManifestValidationError, match="does not satisfy"):
        manifest.check_compatibility(yuki_version="3.8.9")
