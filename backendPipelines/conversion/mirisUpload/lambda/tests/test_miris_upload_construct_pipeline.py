#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import json
import os
import pytest

pytestmark = pytest.mark.unit

# This module's boto3 clients are constructed at import time from AWS_REGION. Set it
# defensively so this test file does not depend on the ambient shell environment.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def construct(monkeypatch):
    monkeypatch.setenv("MIRIS_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("MIRIS_API_KEY_SECRET_ARN", "arn:aws:secretsmanager:::secret:x")
    import importlib
    import constructPipeline
    return importlib.reload(constructPipeline)


def _event(**overrides):
    event = {
        "jobName": "job-1",
        "inputS3AssetFilePath": "s3://assets/a1/sub/model.usd",
        "inputOutputS3AssetAuxiliaryFilesPath": "s3://aux/a1",
        "outputS3AssetFilesPath": "s3://assets/pipelines/p/job/output/E1/files/",
        "outputS3AssetMetadataPath": "s3://assets/pipelines/p/job/output/E1/metadata/",
        "inputConfigurationS3Location": "s3://assets/cfg.json",
        "assetId": "a1",
        "databaseId": "db1",
        "assetRootS3Path": "s3://assets/a1/",
        "mirisAssetName": "Pump Housing",
    }
    event.update(overrides)
    return event


def test_definition_carries_config_location_and_asset_name(construct):
    result = construct.lambda_handler(_event(), None)
    stage = json.loads(result["definition"][0])["stages"][0]

    assert stage["mirisAssetName"] == "Pump Housing"
    assert stage["inputConfigurationS3Location"] == "s3://assets/cfg.json"
    assert stage["assetId"] == "a1"


def test_input_parameters_are_gone(construct):
    result = construct.lambda_handler(_event(), None)
    definition = json.loads(result["definition"][0])

    assert "inputParameters" not in definition
    assert "inputParameters" not in result


def test_no_duplicate_short_circuit_remains(construct):
    # Dedup moved to the gate's atomic claim; construct must never report DUPLICATE_DETECTED.
    first = construct.lambda_handler(_event(), None)
    second = construct.lambda_handler(_event(), None)

    assert first["status"] == "STARTING"
    assert second["status"] == "STARTING"


def test_claim_fields_are_carried_to_the_failure_branch(construct):
    # The state machine's failure branch reads these JSONPaths off the construct result.
    result = construct.lambda_handler(_event(claimFingerprint="fp1"), None)

    assert result["databaseId"] == "db1"
    assert result["assetId"] == "a1"
    assert result["claimFingerprint"] == "fp1"
    assert result["inputOutputS3AssetAuxiliaryFilesPath"] == "s3://aux/a1"


def test_claim_fields_are_present_for_a_run_without_a_claim(construct):
    result = construct.lambda_handler(_event(), None)

    assert result["claimFingerprint"] == ""


@pytest.mark.parametrize("root_uri,expected", [
    ("s3://assets/a1/", {"bucketName": "assets", "objectDir": "a1/"}),
    ("s3://assets/vams/data/a1", {"bucketName": "assets", "objectDir": "vams/data/a1/"}),
])
def test_definition_carries_the_resolved_asset_root(construct, root_uri, expected):
    result = construct.lambda_handler(_event(assetRootS3Path=root_uri), None)
    stage = json.loads(result["definition"][0])["stages"][0]

    assert stage["assetRoot"] == expected


@pytest.mark.parametrize("root_uri", [None, "", "s3://assets", "s3://assets/", "assets/a1/"])
def test_missing_or_unusable_asset_root_fails_before_batch_submit(construct, root_uri):
    event = _event()
    if root_uri is None:
        del event["assetRootS3Path"]
    else:
        event["assetRootS3Path"] = root_uri

    with pytest.raises(ValueError, match="assetRootS3Path"):
        construct.lambda_handler(event, None)


def test_definition_carries_the_root_extensions(construct, monkeypatch):
    monkeypatch.setattr(construct, "ALLOWED_INPUT_FILEEXTENSIONS", ".USD, .usdz")
    result = construct.lambda_handler(_event(), None)
    stage = json.loads(result["definition"][0])["stages"][0]

    assert stage["rootExtensions"] == [".usd", ".usdz"]


def test_definition_carries_the_metadata_output_path(construct):
    result = construct.lambda_handler(_event(), None)
    stage = json.loads(result["definition"][0])["stages"][0]

    assert stage["outputMetadataFiles"] == {
        "bucketName": "assets",
        "objectDir": "pipelines/p/job/output/E1/metadata/",
    }
    # The container writes nothing to the files output path.
    assert "outputFiles" not in stage


def test_metadata_output_prefix_is_normalized_to_a_folder(construct):
    result = construct.lambda_handler(
        _event(outputS3AssetMetadataPath="s3://assets/pipelines/p/job/output/E1/metadata"), None)
    stage = json.loads(result["definition"][0])["stages"][0]

    assert stage["outputMetadataFiles"]["objectDir"] == "pipelines/p/job/output/E1/metadata/"


@pytest.mark.parametrize("metadata_path", [
    None,
    "",
    "s3://assets",
    "s3://assets/",
    "assets/pipelines/p/job/output/E1/metadata/",
])
def test_missing_or_unusable_metadata_output_path_fails_before_batch_submit(construct, metadata_path):
    event = _event()
    if metadata_path is None:
        del event["outputS3AssetMetadataPath"]
    else:
        event["outputS3AssetMetadataPath"] = metadata_path

    with pytest.raises(ValueError, match="outputS3AssetMetadataPath"):
        construct.lambda_handler(event, None)


def test_the_container_definition_carries_no_task_token(construct):
    # The definition is the Batch job's command line, readable by anyone who can describe the job,
    # and the container never reports to Step Functions itself.
    result = construct.lambda_handler(_event(externalSfnTaskToken="tok-secret-1"), None)

    assert "tok-secret-1" not in result["definition"][0]
    # The state machine's end tasks still read it off the state.
    assert result["externalSfnTaskToken"] == "tok-secret-1"
