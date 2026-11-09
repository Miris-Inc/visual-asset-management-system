#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import datetime
import json
import os
import sys

import pytest

_CONTAINER_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _CONTAINER_DIR)

from stream_metadata import (  # noqa: E402
    build_stream_metadata_body,
    format_uploaded_at,
    metadata_object_key,
    metadata_output_location,
)
from miris_upload_container_harness import ASSET_ID, METADATA_PREFIX, definition  # noqa: E402

pytestmark = pytest.mark.unit

UPLOADED_AT = datetime.datetime(2026, 10, 14, 9, 30, 15, 123456, tzinfo=datetime.timezone.utc)


def _body(**overrides):
    kwargs = {
        "asset_uuid": "0b6f4c9e-6a43-4bb5-9d0c-4b3e2b1f7a10",
        "source_version_id": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
        "display_name": "Pump Housing",
        "uploaded_at": UPLOADED_AT,
    }
    kwargs.update(overrides)
    return build_stream_metadata_body(**kwargs)


def _entries(body):
    return {item["metadataKey"]: item for item in body["metadata"]}


# --- body -------------------------------------------------------------------------------------


def test_body_is_a_file_metadata_upsert():
    body = _body()

    assert body["type"] == "metadata"
    assert body["updateType"] == "update"


def test_body_carries_exactly_the_four_stream_keys():
    assert set(_entries(_body())) == {
        "miris_asset_uuid",
        "miris_source_version_id",
        "miris_uploaded_at",
        "miris_display_name",
    }
    assert len(_body()["metadata"]) == 4


def test_body_values_and_value_types():
    entries = _entries(_body())

    assert entries["miris_asset_uuid"] == {
        "metadataKey": "miris_asset_uuid",
        "metadataValue": "0b6f4c9e-6a43-4bb5-9d0c-4b3e2b1f7a10",
        "metadataValueType": "string",
    }
    assert entries["miris_source_version_id"]["metadataValue"] == "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY"
    assert entries["miris_source_version_id"]["metadataValueType"] == "string"
    assert entries["miris_uploaded_at"]["metadataValue"] == "2026-10-14T09:30:15Z"
    assert entries["miris_uploaded_at"]["metadataValueType"] == "date"
    assert entries["miris_display_name"]["metadataValue"] == "Pump Housing"
    assert entries["miris_display_name"]["metadataValueType"] == "string"


def test_uploaded_at_parses_the_way_the_vams_date_validator_parses_it():
    value = _entries(_body())["miris_uploaded_at"]["metadataValue"]

    assert datetime.datetime.fromisoformat(value.replace("Z", "+00:00")) == UPLOADED_AT.replace(
        microsecond=0
    )


def test_uploaded_at_is_rendered_in_utc():
    plus_two = datetime.timezone(datetime.timedelta(hours=2))

    assert format_uploaded_at(datetime.datetime(2026, 10, 14, 11, 30, tzinfo=plus_two)) == (
        "2026-10-14T09:30:00Z"
    )


@pytest.mark.parametrize("version_id", [None, ""])
def test_unknown_source_version_is_written_as_empty_string(version_id):
    entry = _entries(_body(source_version_id=version_id))["miris_source_version_id"]

    assert entry["metadataValue"] == ""


def test_body_is_json_serializable():
    assert json.loads(json.dumps(_body())) == _body()


# --- object key -------------------------------------------------------------------------------


def test_metadata_key_for_a_root_usd():
    assert metadata_object_key(METADATA_PREFIX, "model.usda") == (
        f"{METADATA_PREFIX}model.usda.metadata.json"
    )


def test_metadata_key_for_a_nested_usd():
    assert metadata_object_key(METADATA_PREFIX, "scenes/v2/model.usd") == (
        f"{METADATA_PREFIX}scenes/v2/model.usd.metadata.json"
    )


def test_metadata_key_keeps_spaces_in_the_file_name_verbatim():
    assert metadata_object_key(METADATA_PREFIX, "Coral House.usdz") == (
        f"{METADATA_PREFIX}Coral House.usdz.metadata.json"
    )


# --- metadata output location -----------------------------------------------------------------


def test_metadata_output_location_reads_the_stage():
    stage = {"outputMetadataFiles": {"bucketName": "b", "objectDir": METADATA_PREFIX}}

    assert metadata_output_location(stage) == ("b", METADATA_PREFIX)


def test_metadata_output_location_normalizes_the_prefix_to_a_folder():
    stage = {"outputMetadataFiles": {"bucketName": "b", "objectDir": METADATA_PREFIX.rstrip("/")}}

    assert metadata_output_location(stage) == ("b", METADATA_PREFIX)


@pytest.mark.parametrize(
    "stage",
    [
        {},
        {"outputMetadataFiles": {}},
        {"outputMetadataFiles": {"bucketName": "b", "objectDir": ""}},
        {"outputMetadataFiles": {"bucketName": "", "objectDir": METADATA_PREFIX}},
    ],
)
def test_metadata_output_location_rejects_a_missing_path(stage):
    with pytest.raises(ValueError, match="metadata output path"):
        metadata_output_location(stage)


# --- container entry point --------------------------------------------------------------------


def _usdz_asset(container, rel="pump.usdz", root=f"{ASSET_ID}/"):
    container.s3.add("asset-bucket", f"{root}{rel}", b"usdz")


def test_main_writes_stream_metadata_next_to_the_usd_relative_path(container):
    _usdz_asset(container, "scenes/pump.usdz")
    container.run(definition(trigger_rel="scenes/pump.usdz"))

    container.s3.put_object.assert_called_once()
    put = container.s3.put_object.call_args.kwargs
    assert put["Bucket"] == "asset-bucket"
    assert put["Key"] == f"{METADATA_PREFIX}scenes/pump.usdz.metadata.json"
    assert put["ContentType"] == "application/json"
    entries = _entries(json.loads(put["Body"]))
    assert entries["miris_asset_uuid"]["metadataValue"] == "0b6f4c9e-6a43-4bb5-9d0c-4b3e2b1f7a10"
    assert entries["miris_display_name"]["metadataValue"] == "pump"


def test_main_records_the_version_it_downloaded(container):
    _usdz_asset(container)
    container.run(definition())

    assert container.s3.downloads == [
        ("asset-bucket", f"{ASSET_ID}/pump.usdz", {"VersionId": f"v-{ASSET_ID}/pump.usdz"})]
    body = json.loads(container.s3.put_object.call_args.kwargs["Body"])
    assert _entries(body)["miris_source_version_id"]["metadataValue"] == f"v-{ASSET_ID}/pump.usdz"


def test_main_writes_an_empty_source_version_for_an_unversioned_object(container):
    _usdz_asset(container)
    container.s3.unversioned.add(f"{ASSET_ID}/pump.usdz")
    container.run(definition())

    assert container.s3.downloads == [("asset-bucket", f"{ASSET_ID}/pump.usdz", None)]
    body = json.loads(container.s3.put_object.call_args.kwargs["Body"])
    assert _entries(body)["miris_source_version_id"]["metadataValue"] == ""


def test_main_writes_only_the_metadata_file(container):
    _usdz_asset(container)
    container.run(definition())

    written = [c.kwargs["Key"] for c in container.s3.put_object.call_args_list]
    assert written == [f"{METADATA_PREFIX}pump.usdz.metadata.json"]
    container.s3.upload_file.assert_not_called()


def test_main_fails_before_uploading_when_the_metadata_path_is_missing(container):
    _usdz_asset(container)
    d = json.loads(definition())
    del d["stages"][0]["outputMetadataFiles"]

    with pytest.raises(SystemExit) as exit_info:
        container.run(json.dumps(d))

    assert exit_info.value.code == 7
    container.miris_cls.assert_not_called()
    container.s3.put_object.assert_not_called()


def test_asset_download_pins_only_the_usd_layers(container, monkeypatch):
    root = f"{ASSET_ID}/"
    container.s3.add("asset-bucket", f"{root}scenes/pump.usda", "#usda 1.0")
    container.s3.add("asset-bucket", f"{root}scenes/textures/wood.png", "png")
    monkeypatch.setattr(container.main, "compute_dependencies",
                        lambda path: ([path], [os.path.join(os.path.dirname(path), "wood.png")], []))
    monkeypatch.setattr(container.main, "package_usdz", lambda root_path, out: open(out, "wb").write(b"z"))

    container.run(definition(trigger_rel="scenes/pump.usda"))

    pinned = {key: extra for _, key, extra in container.s3.downloads}
    assert pinned == {
        f"{root}scenes/pump.usda": {"VersionId": f"v-{root}scenes/pump.usda"},
        f"{root}scenes/textures/wood.png": None,
    }
