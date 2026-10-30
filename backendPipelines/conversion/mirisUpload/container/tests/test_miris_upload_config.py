#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from upload_config import resolve_upload_config

pytestmark = pytest.mark.unit


def _s3_returning(body):
    client = MagicMock()
    client.get_object.return_value = {
        "Body": MagicMock(read=lambda: json.dumps(body).encode("utf-8"))}
    return client


def test_template_name_wins_over_asset_name():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json",
             "mirisAssetName": "Pump Housing"}
    cfg = resolve_upload_config(
        stage, "model.usdz", _s3_returning({"mirisAssetName": "Custom Name"}))
    assert cfg.asset_name == "Custom Name"


def test_blank_template_name_falls_back_to_vams_asset_name():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json",
             "mirisAssetName": "Pump Housing"}
    cfg = resolve_upload_config(
        stage, "model.usdz", _s3_returning({"mirisAssetName": ""}))
    assert cfg.asset_name == "Pump Housing"


def test_falls_back_to_filename_when_nothing_supplied():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json", "mirisAssetName": ""}
    cfg = resolve_upload_config(stage, "model.usdz", _s3_returning({}))
    assert cfg.asset_name == "model"


def test_tags_default_to_empty_list():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json", "mirisAssetName": "A"}
    cfg = resolve_upload_config(stage, "model.usdz", _s3_returning({}))
    assert cfg.tags == []


def test_tags_are_read_from_the_rendered_config():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json", "mirisAssetName": "A"}
    cfg = resolve_upload_config(
        stage, "model.usdz", _s3_returning({"mirisTags": ["scan", "wip"]}))
    assert cfg.tags == ["scan", "wip"]


def test_missing_config_location_is_not_fatal():
    stage = {"inputConfigurationS3Location": "", "mirisAssetName": "Pump Housing"}
    cfg = resolve_upload_config(stage, "model.usdz", MagicMock())
    assert cfg.asset_name == "Pump Housing"
    assert cfg.tags == []


@pytest.mark.parametrize("tag", [
    "vams-asset-x86487e0b", "vams-database-db2", "VAMS-ASSET-x", " vams-database-db2"])
def test_reserved_binding_tags_are_dropped_with_a_warning(tag, caplog):
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json", "mirisAssetName": "A"}
    cfg = resolve_upload_config(
        stage, "model.usdz", _s3_returning({"mirisTags": ["scan", tag, "vams"]}))

    assert cfg.tags == ["scan", "vams"]
    assert any(r.levelname == "WARNING" and tag in r.getMessage() for r in caplog.records)


def test_tags_that_only_resemble_the_reserved_prefixes_are_kept():
    stage = {"inputConfigurationS3Location": "s3://b/cfg.json", "mirisAssetName": "A"}
    cfg = resolve_upload_config(stage, "model.usdz", _s3_returning(
        {"mirisTags": ["vams-assets", "my-vams-asset-1", "vams-database"]}))

    assert cfg.tags == ["vams-assets", "my-vams-asset-1", "vams-database"]
