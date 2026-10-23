"""Version consistency: settings constant, appinfo/info.xml version and the
docker image-tag must agree (deployments pull the image tag the manifest
advertises, so a drifted tag deploys the wrong image)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from marp_slides.settings import APP_ID, APP_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_info_xml_version_matches_settings() -> None:
    tree = ET.parse(REPO_ROOT / "appinfo" / "info.xml")
    info = tree.getroot()
    assert info.findtext("id") == APP_ID
    assert info.findtext("version") == APP_VERSION


def test_info_xml_docker_image_tag_matches_version() -> None:
    tree = ET.parse(REPO_ROOT / "appinfo" / "info.xml")
    external_app = tree.getroot().find("external-app")
    assert external_app is not None
    install = external_app.find("docker-install")
    assert install is not None
    assert install.findtext("image-tag") == APP_VERSION
