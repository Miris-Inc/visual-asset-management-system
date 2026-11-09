# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Miris viewer key table stream consumer: only TTL deletions delete a Miris key, a key
Miris no longer has counts as deleted, and every other failure is reported per record under the
stream record's SequenceNumber."""

import logging

import pytest

from backend.backend.handlers.addon.miris import viewerKeyCleanup

KEY_A = "3f2b8c1e-9a4d-4e7f-8b6a-1c2d3e4f5a6b"
KEY_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
TTL_IDENTITY = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}


def _record(key_id, sequence_number, event_name="REMOVE", user_identity=TTL_IDENTITY):
    record = {
        "eventID": f"event-{sequence_number}",
        "eventName": event_name,
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "Keys": {"mirisViewerKeyId": {"S": key_id}},
            "SequenceNumber": sequence_number,
            "StreamViewType": "KEYS_ONLY",
        },
    }
    if user_identity is not None:
        record["userIdentity"] = user_identity
    return record


def _invoke(*records):
    return viewerKeyCleanup.lambda_handler({"Records": list(records)}, None)


def _deleted_urls(miris_api):
    return [c["url"] for c in miris_api.calls if c["method"] == "DELETE"]


def test_a_ttl_removal_deletes_the_key_once(miris_api):
    miris_api.responses.append(204)

    result = _invoke(_record(KEY_A, "100"))

    assert result == {"batchItemFailures": []}
    assert _deleted_urls(miris_api) == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_A}"]


def test_each_ttl_removal_in_a_batch_is_deleted(miris_api):
    miris_api.responses.extend([204, 200])

    result = _invoke(_record(KEY_A, "100"), _record(KEY_B, "101"))

    assert result == {"batchItemFailures": []}
    assert [url.rsplit("/", 1)[1] for url in _deleted_urls(miris_api)] == [KEY_A, KEY_B]


def test_records_that_are_not_ttl_removals_are_ignored(miris_api):
    result = _invoke(
        _record(KEY_A, "100", user_identity=None),  # a user or application delete
        _record(KEY_A, "101", event_name="INSERT", user_identity=None),
        _record(KEY_A, "102", event_name="MODIFY", user_identity=None),
        _record(KEY_A, "103", user_identity={"type": "Service", "principalId": "other.amazonaws.com"}),
        _record(KEY_A, "104", user_identity={"type": "User", "principalId": "dynamodb.amazonaws.com"}),
        _record(KEY_A, "105", event_name="INSERT"),
    )

    assert result == {"batchItemFailures": []}
    assert miris_api.calls == []
    miris_api.secretsmanager.get_secret_value.assert_not_called()


def test_a_key_miris_no_longer_has_counts_as_deleted(miris_api):
    miris_api.responses.append(404)

    assert _invoke(_record(KEY_A, "100")) == {"batchItemFailures": []}
    assert len(miris_api.calls) == 1


def test_a_server_error_reports_only_that_record(miris_api):
    miris_api.responses.extend([503, 204])

    result = _invoke(_record(KEY_A, "100"), _record(KEY_B, "101"))

    assert result == {"batchItemFailures": [{"itemIdentifier": "100"}]}
    assert len(miris_api.calls) == 2


def test_a_transport_failure_reports_the_record(miris_api):
    import requests

    miris_api.responses.append(requests.ConnectionError("down"))

    assert _invoke(_record(KEY_A, "100")) == {"batchItemFailures": [{"itemIdentifier": "100"}]}


def test_an_invalid_key_id_is_skipped_without_an_http_call(miris_api, log_recorder):
    # A retry cannot repair a malformed key ID, so it is not reported as a batch item failure.
    miris_api.responses.append(204)

    result = _invoke(
        _record("../not-a-key", "100"),
        _record(None, "101"),
        _record(KEY_A, "102"),
    )

    assert result == {"batchItemFailures": []}
    assert _deleted_urls(miris_api) == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_A}"]
    text = log_recorder.text()
    assert "Skipping stream record 100" in text
    assert "Skipping stream record 101" in text


def _without_sequence_number(key_id, event_id):
    record = _record(key_id, "unused")
    del record["dynamodb"]["SequenceNumber"]
    record["eventID"] = event_id
    return record


def test_a_record_without_a_sequence_number_is_still_deleted(miris_api):
    miris_api.responses.append(204)

    assert _invoke(_without_sequence_number(KEY_A, "event-a")) == {"batchItemFailures": []}
    assert _deleted_urls(miris_api) == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_A}"]


def test_a_failed_record_without_a_sequence_number_retries_the_whole_batch(miris_api, log_recorder):
    miris_api.responses.extend([503, 204, 503])

    with pytest.raises(viewerKeyCleanup.UnreportableRecordFailure):
        _invoke(
            _without_sequence_number(KEY_A, "event-a"),
            _record(KEY_B, "101"),
            _record(KEY_A, "102"),
        )

    # Every record in the batch was still attempted before the batch failed.
    assert len(miris_api.calls) == 3
    assert "Stream record event-a has no SequenceNumber" in log_recorder.text()


def test_an_invalid_record_without_a_sequence_number_is_skipped_by_event_id(miris_api, log_recorder):
    assert _invoke(_without_sequence_number("not-a-key", "event-x")) == {"batchItemFailures": []}
    assert miris_api.calls == []
    assert "Skipping stream record event-x" in log_recorder.text()


def test_an_empty_event_reports_no_failures(miris_api):
    assert viewerKeyCleanup.lambda_handler({}, None) == {"batchItemFailures": []}
    assert viewerKeyCleanup.lambda_handler({"Records": []}, None) == {"batchItemFailures": []}


def test_no_secret_or_response_body_reaches_the_logs(miris_api, log_recorder, caplog):
    caplog.set_level(logging.DEBUG)
    body = "miris-error-body-must-not-surface"
    miris_api.responses.extend([204, 404, (500, body), (401, body), 204])

    _invoke(
        _record(KEY_A, "100"),
        _record(KEY_B, "101"),
        _record(KEY_A, "102"),
        _record(KEY_B, "103"),
        _record("not-a-key", "104"),
    )

    # Control: the deletes, the 404, the failure and the invalid record were all logged.
    text = log_recorder.text()
    assert f"Deleted Miris viewer key {KEY_A}" in text
    assert "already deleted" in text
    assert "HTTP 500" in text
    assert "104" in text
    for logged in (text, caplog.text):
        assert miris_api.integration_key not in logged
        assert body not in logged
