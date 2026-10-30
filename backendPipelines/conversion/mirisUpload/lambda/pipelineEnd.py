#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Step Functions Task Token callback. Reports back to the outer VAMS workflow.

Invoked two ways: by the inner state machine's own end states, and by an Amazon EventBridge rule on
the inner execution's FAILED, TIMED_OUT and ABORTED status changes. The rule covers the ends no state
inside the execution can reach, a state machine timeout or a stopped execution, which would otherwise
leave the outer workflow task waiting out its own timeout and the claim in place.

A run that does not succeed also releases the dedup claim the gate wrote, so the same content is
eligible again for the next trigger. A successful run keeps its claim: that record is what collapses
the per-file trigger fan-out and repeat triggers on unchanged content. It also retires the asset's
claims on other content, so a later trigger on earlier content (a reverted version) uploads again.
"""
import json
import os

import boto3
from botocore.config import Config
from customLogging.logger import safeLogger

import mirisClaim

logger = safeLogger(service="MirisUploadPipelineEnd")
retry_config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
sfn = boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"], config=retry_config)
s3_client = boto3.client("s3", region_name=os.environ["AWS_REGION"], config=retry_config)

EXECUTION_STATUS_DETAIL_TYPE = "Step Functions Execution Status Change"
FAILURE_ERROR = "MirisUploadFailed"
CAUSE_MAX_CHARS = 256

# Error codes Step Functions returns for a token that has already been reported, timed out, or
# belongs to a workflow that no longer exists. The EventBridge route reaches a FAILED execution
# whose failure branch already reported, so these are the expected outcome there.
CLOSED_TOKEN_ERROR_CODES = ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken")

# The container's exit codes (EXIT_* in container/__main__.py) and the reason the workflow shows.
EXIT_CODE_REASONS = {
    1: "The Miris upload container failed with an unexpected error. See the container log.",
    2: "The Miris upload container was started without a complete pipeline definition.",
    3: "The file is not a USD format the Miris upload accepts (.usd, .usda, .usdc or .usdz).",
    4: "The asset is larger than the configured maximum upload size "
       "(app.addons.useMirisStreaming.upload.maxAssetSizeBytes).",
    5: "The USD file references layers or assets that are not part of the VAMS asset.",
    6: "The asset has no USD file to upload, or every USD file in it is referenced by another.",
    7: "The run has no metadata output location for the Miris stream metadata.",
    8: "Miris rejected the upload or could not process it. See the container log.",
    9: "Miris did not finish processing the asset within the configured task timeout "
       "(app.addons.useMirisStreaming.upload.taskTimeoutSeconds).",
    10: "The asset has more than one root USD file (ambiguous_root): no other USD file in the asset "
        "references them. Keep one root layer per asset, or reference the others from it.",
    11: "A USD file in the asset is empty or malformed and could not be read (unreadable_layer). "
        "The container log's unreadable_layer entry names the file; fix or remove it.",
    12: "The USD file references a layer or asset outside the VAMS asset (reference_outside_asset), "
        "which is never packaged. The container log's reference_outside_asset entry names the files; "
        "reference files inside the asset with relative paths.",
}

EXECUTION_STATUS_REASONS = {
    "TIMED_OUT": "The Miris upload did not finish within the pipeline's time limit.",
    "ABORTED": "The Miris upload was stopped before it finished.",
    "FAILED": "The Miris upload pipeline failed. See the pipeline's state machine log.",
}

CONTAINER_TIME_LIMIT_REASON = "The Miris upload container exceeded its time limit and was stopped."


def _parse_json(text):
    if isinstance(text, dict):
        return text
    try:
        parsed = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _batch_exit_code(job):
    """The container exit code from a Batch job description, or None when the container never ran."""
    code = (job.get("Container") or {}).get("ExitCode")
    if code is None:
        attempts = job.get("Attempts") or []
        if attempts:
            code = (attempts[-1].get("Container") or {}).get("ExitCode")
    return code


def failure_reason(error_type, error_cause):
    """A user-facing reason for a failed state: the Batch job's exit code, a Lambda error message,
    or a generic reason. Never the raw Batch job description."""
    cause = _parse_json(error_cause)
    if cause is not None and ("Container" in cause or "JobId" in cause):
        code = _batch_exit_code(cause)
        if code in EXIT_CODE_REASONS:
            return EXIT_CODE_REASONS[code]
        status_reason = cause.get("StatusReason") or (cause.get("Container") or {}).get("Reason") or ""
        if "timeout" in status_reason.lower() or "duration" in status_reason.lower():
            return CONTAINER_TIME_LIMIT_REASON
        if code is not None:
            return f"The Miris upload container exited with code {code}. See the container log."
        if status_reason:
            return f"The Miris upload container did not run: {status_reason}"
        return "The Miris upload container did not complete. See the container log."
    if cause is not None and cause.get("errorMessage"):
        return f"The Miris upload could not be prepared: {cause['errorMessage']}"
    if error_type == "States.Timeout":
        return EXECUTION_STATUS_REASONS["TIMED_OUT"]
    return "The Miris upload failed. See the pipeline's state machine log."


def _report_failure(task_token, reason):
    try:
        sfn.send_task_failure(taskToken=task_token, error=FAILURE_ERROR, cause=reason[:CAUSE_MAX_CHARS])
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in CLOSED_TOKEN_ERROR_CODES:
            logger.info(f"Workflow task already closed ({code}); nothing to report")
            return
        raise


def _release_claim(state, task_token):
    """Release the gate's claim if this execution still holds it."""
    mirisClaim.release_claim(
        s3_client,
        logger,
        state.get("inputOutputS3AssetAuxiliaryFilesPath", ""),
        state.get("databaseId", ""),
        state.get("assetId", ""),
        state.get("claimFingerprint", ""),
        mirisClaim.owner_id(task_token),
    )


def _handle_execution_status_change(event):
    """The EventBridge route: the inner execution ended without its own end state reporting."""
    detail = event.get("detail") or {}
    status = detail.get("status", "")
    execution_input = _parse_json(detail.get("input"))
    logger.info(f"Inner execution {detail.get('executionArn', '')} ended with status {status}")
    if execution_input is None:
        logger.warning("Execution status event carried no readable input; nothing to report")
        return {"statusCode": 200, "body": "no-op"}

    task_token = execution_input.get("externalSfnTaskToken", "")
    _release_claim(execution_input, task_token)
    if task_token:
        _report_failure(task_token, EXECUTION_STATUS_REASONS.get(status, EXECUTION_STATUS_REASONS["FAILED"]))
    return {"statusCode": 200, "body": "ok"}


def lambda_handler(event, context):
    if event.get("detail-type") == EXECUTION_STATUS_DETAIL_TYPE:
        return _handle_execution_status_change(event)

    logger.info("MirisUploadPipelineEnd received event")
    task_token = event.get("externalSfnTaskToken")
    status = event.get("status", "success")

    if status == "success":
        mirisClaim.retire_superseded_claims(
            s3_client,
            logger,
            event.get("inputOutputS3AssetAuxiliaryFilesPath", ""),
            event.get("databaseId", ""),
            event.get("assetId", ""),
            event.get("claimFingerprint", ""),
        )
    else:
        _release_claim(event, task_token)

    if not task_token:
        logger.warning("No external task token in event; nothing to call back to")
        return {"statusCode": 200, "body": "no-op"}

    if status == "success":
        sfn.send_task_success(taskToken=task_token, output=json.dumps({"status": "success"}))
    else:
        _report_failure(task_token, failure_reason(event.get("errorType", ""), event.get("errorCause", "")))

    return {"statusCode": 200, "body": "ok"}
