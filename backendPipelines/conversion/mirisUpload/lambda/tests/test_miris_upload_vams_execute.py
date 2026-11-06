#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from miris_upload_fakes import claim_held_by

pytestmark = pytest.mark.unit

# This module's boto3 clients are constructed at import time from AWS_REGION. Set it
# defensively so this test file does not depend on the ambient shell environment.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

FP = "f" * 64


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("OPEN_PIPELINE_FUNCTION_NAME", "open-fn")
    monkeypatch.setenv("MIRIS_UPLOAD_GATE_FUNCTION_NAME", "gate-fn")
    monkeypatch.setenv("ASSET_STORAGE_TABLE_NAME", "asset-table")
    import importlib
    import vamsExecuteMirisUpload
    return importlib.reload(vamsExecuteMirisUpload)


def _resolved(**overrides):
    resolved = {
        "assetId": "a1",
        "databaseId": "db1",
        "inputS3AssetFilePath": "s3://b/vams/data/a1/model.usd",
        "outputS3AssetFilesPath": "s3://b/vams/data/a1/",
        "outputS3AssetPreviewPath": "",
        "outputS3AssetMetadataPath": "",
        "inputOutputS3AssetAuxiliaryFilesPath":
            "s3://vams-aux-bucket/pipelines/miris-upload/exec-aaaa1111/",
        "inputMetadataS3Location": "",
        "inputConfigurationS3Location": "s3://b/cfg.json",
        "orchestrationEventPrefix": "",
        "inputFiles": [{"assetId": "a1", "bucket": "b", "assetRootS3Key": "vams/data/a1"}],
        "manifestUsed": True,
        "inputVersionId": "",
    }
    resolved.update(overrides)
    return resolved


def _asset_table_mock(asset_name="Pump Housing"):
    table = MagicMock()
    table.get_item.return_value = {"Item": {"assetName": asset_name}}
    return table


def _gate_says(gate="proceed", fingerprint=FP):
    return {
        "StatusCode": 200,
        "Payload": MagicMock(read=lambda: json.dumps(
            {"gate": gate, "claimFingerprint": fingerprint}).encode()),
    }


def _configuration(body):
    """An S3 client whose rendered configuration file holds `body` (a dict, or None for none)."""
    s3 = MagicMock()
    if body is None:
        s3.get_object.side_effect = Exception("NoSuchKey")
    else:
        s3.get_object.side_effect = lambda **_: {"Body": io.BytesIO(json.dumps(body).encode())}
    return s3


def _run(handler, resolved=None, s3=None, executing_user="SYSTEM_USER", gate=None,
         open_result=None):
    """Run the handler; returns (response, gate body, open payload or None)."""
    with patch.object(handler.manifestHelper, "resolve_pipeline_inputs",
                      return_value=resolved or _resolved()), \
         patch.object(handler, "_asset_table", return_value=_asset_table_mock()), \
         patch.object(handler, "s3_client", s3 or _configuration(None)), \
         patch.object(handler, "sfn_client"), \
         patch.object(handler, "lambda_client") as lc:
        lc.invoke.side_effect = [gate or _gate_says(), open_result or {"StatusCode": 200}]
        resp = handler.lambda_handler({"body": json.dumps(
            {"TaskToken": "tok-1", "executingUserName": executing_user})}, None)
    calls = lc.invoke.call_args_list
    gate_body = json.loads(json.loads(calls[0].kwargs["Payload"].decode())["body"]) if calls else None
    open_payload = json.loads(calls[1].kwargs["Payload"].decode()) if len(calls) > 1 else None
    return resp, gate_body, open_payload


def test_missing_task_token_reports_nothing_and_500s(handler):
    # No token to report against, so the handler must not raise on the way out.
    resp = handler.lambda_handler({"body": json.dumps({"assetId": "a1"})}, None)
    assert resp["statusCode"] == 500


def test_manifest_failure_sends_task_failure(handler):
    with patch.object(handler, "sfn_client") as sfn, \
         patch.object(handler.manifestHelper, "resolve_pipeline_inputs",
                      side_effect=Exception("manifest unreadable")):
        resp = handler.lambda_handler(
            {"body": json.dumps({"TaskToken": "tok-1"})}, None)

    assert resp["statusCode"] == 500
    sfn.send_task_failure.assert_called_once()
    assert sfn.send_task_failure.call_args.kwargs["taskToken"] == "tok-1"


def test_the_gate_receives_the_asset_root_and_name(handler):
    resp, gate_body, _ = _run(handler)

    assert resp["statusCode"] == 200
    assert gate_body["assetBucket"] == "b"
    assert gate_body["assetRootS3Key"] == "vams/data/a1/"
    assert gate_body["mirisAssetName"] == "Pump Housing"


def test_gate_skip_short_circuits(handler):
    with patch.object(handler.manifestHelper, "resolve_pipeline_inputs", return_value=_resolved()), \
         patch.object(handler, "_asset_table", return_value=_asset_table_mock()), \
         patch.object(handler, "s3_client", _configuration(None)) as s3, \
         patch.object(handler, "lambda_client") as lc:
        lc.invoke.return_value = _gate_says("skip", "")
        resp = handler.lambda_handler({"body": json.dumps({"TaskToken": "tok-1"})}, None)

    assert resp["statusCode"] == 200
    # Only the gate was invoked; openPipeline must not run, and the winner's claim stays.
    assert lc.invoke.call_count == 1
    s3.delete_object.assert_not_called()


def test_the_claim_fingerprint_and_asset_root_reach_the_pipeline(handler):
    """openPipeline (and everything downstream) needs the fingerprint to release the claim, and the
    container lists the asset under the resolved root."""
    _, _, open_payload = _run(handler)

    assert open_payload["claimFingerprint"] == FP
    assert open_payload["assetRootS3Path"] == "s3://b/vams/data/a1/"
    assert open_payload["databaseId"] == "db1"


def test_failure_after_claim_releases_it(handler):
    s3 = claim_held_by(MagicMock(), "tok-1")
    resp, _, _ = _run(handler, s3=s3, open_result=Exception("openPipeline unreachable"))

    assert resp["statusCode"] == 500
    s3.delete_object.assert_called_once_with(
        Bucket="vams-aux-bucket", Key=f"locks/miris-upload/db1/a1/{FP}.claim")


def test_failure_before_the_gate_releases_nothing(handler):
    with patch.object(handler.manifestHelper, "resolve_pipeline_inputs",
                      side_effect=Exception("manifest unreadable")), \
         patch.object(handler, "s3_client") as s3, \
         patch.object(handler, "sfn_client"):
        handler.lambda_handler({"body": json.dumps({"TaskToken": "tok-1"})}, None)

    s3.delete_object.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"inputFiles": []},
    {"inputFiles": [{"assetId": "a1", "bucket": "b", "assetRootS3Key": ""}]},
    {"inputFiles": [{"assetId": "a1", "bucket": "", "assetRootS3Key": "a1/"}]},
    {"databaseId": ""},
    {"assetId": ""},
], ids=["no-input-file", "no-root", "no-bucket", "no-database", "no-asset"])
def test_a_run_without_an_asset_root_or_identity_fails_before_the_gate(handler, overrides):
    with patch.object(handler.manifestHelper, "resolve_pipeline_inputs",
                      return_value=_resolved(**overrides)), \
         patch.object(handler, "_asset_table", return_value=_asset_table_mock()), \
         patch.object(handler, "s3_client", _configuration(None)), \
         patch.object(handler, "sfn_client") as sfn, \
         patch.object(handler, "lambda_client") as lc:
        resp = handler.lambda_handler({"body": json.dumps({"TaskToken": "tok-1"})}, None)

    assert resp["statusCode"] == 500
    lc.invoke.assert_not_called()
    sfn.send_task_failure.assert_called_once()


# --- manual runs ------------------------------------------------------------------------------


@pytest.mark.parametrize("configuration,executing_user,manual", [
    ({"triggerType": "Manual"}, "SYSTEM_USER", True),
    ({"triggerType": "File-Upload"}, "alice", False),
    (None, "SYSTEM_USER", False),
    (None, "alice", True),
    ({"mirisTags": []}, "alice", True),
    ({"triggerType": "{{triggerType}}"}, "SYSTEM_USER", False),
    ({"triggerType": ""}, "alice", True),
], ids=["rendered-manual", "rendered-file-upload", "trigger-user", "user", "no-trigger-type",
        "unrendered-tag", "blank-trigger-type"])
def test_the_manual_flag_reaches_the_gate(handler, configuration, executing_user, manual):
    _, gate_body, _ = _run(handler, s3=_configuration(configuration), executing_user=executing_user)

    assert gate_body["manual"] is manual


def test_a_malformed_configuration_falls_back_to_the_executing_user(handler):
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **_: {"Body": io.BytesIO(b"not json")}
    _, gate_body, _ = _run(handler, s3=s3, executing_user="SYSTEM_USER")

    assert gate_body["manual"] is False


# --- same-Region only -------------------------------------------------------------------------


def _regional(input_region="", output_region=""):
    return _resolved(
        inputFiles=[{"assetId": "a1", "bucket": "b", "assetRootS3Key": "vams/data/a1",
                     "bucketRegion": input_region}],
        outputBucketRegion=output_region)


def _refused(handler, monkeypatch, deployment_region, resolved):
    """Run the handler on `resolved`; returns (response, send_task_failure mock, invoke mock, s3)."""
    monkeypatch.setenv("AWS_REGION", deployment_region)
    with patch.object(handler.manifestHelper, "resolve_pipeline_inputs", return_value=resolved), \
         patch.object(handler, "_asset_table", return_value=_asset_table_mock()), \
         patch.object(handler, "s3_client", _configuration(None)) as s3, \
         patch.object(handler, "sfn_client") as sfn, \
         patch.object(handler, "lambda_client") as lc:
        resp = handler.lambda_handler({"body": json.dumps({"TaskToken": "tok-1"})}, None)
    return resp, sfn.send_task_failure, lc.invoke, s3


@pytest.mark.parametrize("deployment,remote", [
    ("us-east-1", "eu-west-1"),
    ("eu-west-1", "ap-southeast-2"),
    ("ap-southeast-2", "eu-west-1"),
])
@pytest.mark.parametrize("placement", ["input", "output", "both"])
def test_a_cross_region_bucket_fails_the_task_with_the_whole_miris_reason(
        handler, monkeypatch, deployment, remote, placement):
    resolved = _regional(remote if placement in ("input", "both") else "",
                         remote if placement in ("output", "both") else "")
    resp, send_failure, invoke, s3 = _refused(handler, monkeypatch, deployment, resolved)

    assert resp["statusCode"] == 500
    # Rejected before the gate claims anything, so there is no claim to release and nothing starts.
    invoke.assert_not_called()
    s3.delete_object.assert_not_called()
    send_failure.assert_called_once()
    assert send_failure.call_args.kwargs["taskToken"] == "tok-1"

    cause = send_failure.call_args.kwargs["cause"]
    expected = handler.same_region_reason(resolved, deployment)
    # The whole reason reaches the workflow: the 256-character cut-off never truncates it.
    assert len(expected) < 256, (len(expected), expected)
    assert cause == expected
    assert f"must be in {deployment}" in cause
    assert "Miris upload container" in cause
    assert "third-party" not in cause
    if placement in ("input", "both"):
        assert f"the input bucket is in {remote}" in cause
    if placement in ("output", "both"):
        assert f"the output bucket is in {remote}" in cause
    if placement == "input":
        assert "output bucket" not in cause.split(", but ")[1]
    if placement == "output":
        assert "input bucket" not in cause.split(", but ")[1]


def test_same_region_buckets_reach_the_gate(handler, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    resp, gate_body, open_payload = _run(handler, resolved=_regional("us-east-1", "us-east-1"))

    assert resp["statusCode"] == 200
    assert gate_body["assetId"] == "a1"
    assert open_payload is not None


@pytest.mark.parametrize("version", ["v-launch", "null", ""])
def test_the_launch_input_version_reaches_the_pipeline(handler, version):
    _, _, open_payload = _run(handler, resolved=_resolved(inputVersionId=version))

    assert open_payload["inputVersionId"] == version
