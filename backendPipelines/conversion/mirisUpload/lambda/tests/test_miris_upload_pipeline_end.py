#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import json
import os
from unittest.mock import patch

import pytest

import mirisClaim
from miris_upload_fakes import claim_held_by

pytestmark = pytest.mark.unit

# This module's boto3 clients are constructed at import time from AWS_REGION. Set it
# defensively so this test file does not depend on the ambient shell environment.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

AUX = "s3://vams-aux-bucket/pipelines/miris-upload/exec-aaaa1111/"
FP = "f" * 64
CLAIM_KEY = f"locks/miris-upload/db1/a1/{FP}.claim"


def _batch_cause(exit_code=None, status_reason="Essential container in task exited"):
    """The Cause a failed .sync BatchSubmitJob state carries: the Batch job description as JSON."""
    container = {"Image": "x", "LogStreamName": "MirisUploadJob/default/abc"}
    if exit_code is not None:
        container["ExitCode"] = exit_code
    return json.dumps({
        "JobId": "job-1",
        "JobName": "MirisUpload_x",
        "Status": "FAILED",
        "StatusReason": status_reason,
        "Container": container,
        "Attempts": [{"Container": dict(container)}],
    })


@pytest.fixture
def end():
    import importlib
    import pipelineEnd
    return importlib.reload(pipelineEnd)


def _event(**overrides):
    event = {
        "externalSfnTaskToken": "tok-1",
        "status": "success",
        "databaseId": "db1",
        "assetId": "a1",
        "claimFingerprint": FP,
        "inputOutputS3AssetAuxiliaryFilesPath": AUX,
    }
    event.update(overrides)
    return event


def test_success_reports_task_success_and_keeps_the_claim(end):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        end.lambda_handler(_event(), None)

    output = json.loads(sfn.send_task_success.call_args.kwargs["output"])
    assert output == {"status": "success"}
    # The claim is what collapses the trigger fan-out; a successful run must not remove it.
    s3.delete_object.assert_not_called()


def test_failure_reports_task_failure_and_releases_the_claim(end):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        claim_held_by(s3, "tok-1")
        end.lambda_handler(_event(status="failed", errorType="States.TaskFailed",
                                  errorCause=_batch_cause(4)), None)

    sfn.send_task_success.assert_not_called()
    assert sfn.send_task_failure.call_args.kwargs["taskToken"] == "tok-1"
    assert sfn.send_task_failure.call_args.kwargs["cause"] == end.EXIT_CODE_REASONS[4]
    s3.delete_object.assert_called_once_with(
        Bucket="vams-aux-bucket", Key=CLAIM_KEY)


def test_failure_without_a_token_releases_a_claim_recorded_without_an_owner(end):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        claim_held_by(s3, "")
        end.lambda_handler(_event(status="failed", externalSfnTaskToken=""), None)

    sfn.send_task_failure.assert_not_called()
    s3.delete_object.assert_called_once()


@pytest.mark.parametrize("route", ["failure_branch", "execution_ended"])
def test_a_claim_now_held_by_another_execution_is_left_in_place(end, route):
    # Run A failed and was reported once already; retried run B holds a new claim at the same key.
    event = (_event(status="failed") if route == "failure_branch"
             else _status_event("FAILED"))
    with patch.object(end, "sfn"), patch.object(end, "s3_client") as s3:
        claim_held_by(s3, "tok-run-b")
        end.lambda_handler(event, None)

    s3.get_object.assert_called_once_with(
        Bucket="vams-aux-bucket", Key=CLAIM_KEY)
    s3.delete_object.assert_not_called()


def test_the_second_report_of_one_failure_finds_no_claim_and_deletes_nothing(end):
    from botocore.exceptions import ClientError

    with patch.object(end, "sfn"), patch.object(end, "s3_client") as s3:
        s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject")
        end.lambda_handler(_status_event("FAILED"), None)

    s3.delete_object.assert_not_called()


def test_failure_cause_is_truncated(end):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client"):
        end.lambda_handler(_event(status="failed", errorCause=json.dumps({"errorMessage": "x" * 5000})), None)

    assert len(sfn.send_task_failure.call_args.kwargs["cause"]) == 256


def test_release_failure_does_not_mask_the_pipeline_failure(end):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        claim_held_by(s3, "tok-1")
        s3.delete_object.side_effect = Exception("AccessDenied")
        result = end.lambda_handler(_event(status="failed"), None)

    assert result["statusCode"] == 200
    sfn.send_task_failure.assert_called_once()


@pytest.mark.parametrize("exit_code", sorted([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]))
def test_every_container_exit_code_maps_to_a_reason_not_the_job_description(end, exit_code):
    cause = _batch_cause(exit_code)
    reason = end.failure_reason("States.TaskFailed", cause)

    assert reason == end.EXIT_CODE_REASONS[exit_code]
    assert "JobId" not in reason and "{" not in reason


def test_batch_attempt_timeout_reads_as_a_time_limit(end):
    reason = end.failure_reason(
        "States.TaskFailed", _batch_cause(None, "Job attempt duration exceeded timeout"))

    assert reason == end.CONTAINER_TIME_LIMIT_REASON


def test_unmapped_exit_code_names_the_code_only(end):
    reason = end.failure_reason("States.TaskFailed", _batch_cause(137))

    assert "137" in reason and "JobId" not in reason


def test_container_that_never_started_reports_the_batch_status_reason(end):
    reason = end.failure_reason(
        "States.TaskFailed", _batch_cause(None, "CannotPullContainerError: pull access denied"))

    assert reason.endswith("CannotPullContainerError: pull access denied")


def test_construct_lambda_failure_reports_its_message(end):
    cause = json.dumps({"errorMessage": "outputS3AssetMetadataPath is missing", "errorType": "ValueError"})
    reason = end.failure_reason("ValueError", cause)

    assert reason == "The Miris upload could not be prepared: outputS3AssetMetadataPath is missing"


def _status_event(status, **input_overrides):
    execution_input = {
        "externalSfnTaskToken": "tok-1",
        "databaseId": "db1",
        "assetId": "a1",
        "claimFingerprint": FP,
        "inputOutputS3AssetAuxiliaryFilesPath": AUX,
    }
    execution_input.update(input_overrides)
    return {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:sm:MirisUpload_x",
            "status": status,
            "input": json.dumps(execution_input),
        },
    }


@pytest.mark.parametrize("status", ["TIMED_OUT", "ABORTED", "FAILED"])
def test_execution_status_event_releases_the_claim_and_fails_the_outer_token(end, status):
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        claim_held_by(s3, "tok-1")
        result = end.lambda_handler(_status_event(status), None)

    assert result["statusCode"] == 200
    s3.delete_object.assert_called_once_with(
        Bucket="vams-aux-bucket", Key=CLAIM_KEY)
    assert sfn.send_task_failure.call_args.kwargs["taskToken"] == "tok-1"
    assert sfn.send_task_failure.call_args.kwargs["cause"] == end.EXECUTION_STATUS_REASONS[status]
    sfn.send_task_success.assert_not_called()


@pytest.mark.parametrize("code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_an_already_closed_outer_token_is_not_an_error(end, code):
    from botocore.exceptions import ClientError

    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client"):
        sfn.send_task_failure.side_effect = ClientError(
            {"Error": {"Code": code, "Message": "closed"}}, "SendTaskFailure")
        result = end.lambda_handler(_status_event("FAILED"), None)

    assert result["statusCode"] == 200


def test_any_other_callback_error_propagates_so_the_event_is_retried(end):
    from botocore.exceptions import ClientError

    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client"):
        sfn.send_task_failure.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "SendTaskFailure")
        with pytest.raises(ClientError):
            end.lambda_handler(_status_event("TIMED_OUT"), None)


def test_execution_status_event_without_input_is_a_no_op(end):
    event = _status_event("ABORTED")
    del event["detail"]["input"]
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        result = end.lambda_handler(event, None)

    assert result["body"] == "no-op"
    sfn.send_task_failure.assert_not_called()
    s3.delete_object.assert_not_called()


def test_a_failed_run_without_a_claim_reads_and_deletes_nothing(end):
    """A manual run writes no claim, so its failure has none to release."""
    with patch.object(end, "sfn") as sfn, patch.object(end, "s3_client") as s3:
        end.lambda_handler(_event(status="failed", claimFingerprint=""), None)

    s3.get_object.assert_not_called()
    s3.delete_object.assert_not_called()
    sfn.send_task_failure.assert_called_once()


def test_an_ambiguous_root_names_the_reason(end):
    reason = end.failure_reason("States.TaskFailed", _batch_cause(10))

    assert "ambiguous_root" in reason
    assert len(reason) <= end.CAUSE_MAX_CHARS


def test_an_unreadable_layer_points_at_the_log_entry_naming_the_file(end):
    reason = end.failure_reason("States.TaskFailed", _batch_cause(11))

    assert "unreadable_layer" in reason
    assert len(reason) <= end.CAUSE_MAX_CHARS


def test_a_reference_outside_the_asset_points_at_the_log_entry_naming_the_files(end):
    reason = end.failure_reason("States.TaskFailed", _batch_cause(12))

    assert "reference_outside_asset" in reason
    assert len(reason) <= end.CAUSE_MAX_CHARS
