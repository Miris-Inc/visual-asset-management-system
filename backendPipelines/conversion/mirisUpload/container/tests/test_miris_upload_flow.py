#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""The container's upload flow: a failure after the Miris asset exists keeps it in Miris and logs its
id, and every way the flow can end maps to an exit code the pipelineEnd lambda turns into a
user-facing reason."""
import ast
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import responses

pytestmark = pytest.mark.unit

_CONTAINER = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _CONTAINER)

UUID = "11111111-2222-3333-4444-555555555555"
BASE = "https://app.miris.test"


def _load_entry_point():
    """The container's __main__.py under a private name (`import __main__` is pytest's own)."""
    spec = importlib.util.spec_from_file_location(
        "miris_upload_entry_point", os.path.join(_CONTAINER, "__main__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def entry():
    return _load_entry_point()


def _start_response():
    return {
        "id": UUID,
        "upload_path": "s3://miris-uploads/ws/up",
        "endpoint_url": "https://s3.example.test",
        "region": "us-west-1",
        "access_key_id": "AK",
        "secret_key": "SK",
        "session_token": "ST",
    }


def _client():
    from miris_uploader import MirisClient

    client = MagicMock(spec=MirisClient)
    client.start_upload.return_value = _start_response()
    client.poll_until_terminal.return_value = {"state": "preview"}
    client.trigger_generate_best_effort.return_value = {"state": "processing_streamable"}
    return client


def _upload_args(write_metadata=None, task_timeout=60):
    return dict(
        name="Pump",
        tags=["vams"],
        upload_local="/workdir/pump.usdz",
        upload_filename="pump.usdz",
        content_type="model/vnd.usdz+zip",
        size=10,
        task_timeout=task_timeout,
        write_metadata=write_metadata or MagicMock(),
    )


def _run(entry, client, write_metadata=None):
    return entry.run_miris_upload(client, **_upload_args(write_metadata))


def _kept_asset_logs(output):
    """The miris_asset_kept log lines in the container's output, parsed."""
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("msg") == "miris_asset_kept"]


def test_a_successful_upload_keeps_the_asset(entry, capsys):
    client = _client()
    write_metadata = MagicMock()
    with patch.object(entry, "_put_artifact"):
        assert _run(entry, client, write_metadata) == UUID

    write_metadata.assert_called_once_with(UUID)
    assert _kept_asset_logs(capsys.readouterr().out) == []


@pytest.mark.parametrize("failing_step", [
    "put_artifact", "mark_upload_complete", "poll_until_terminal", "write_metadata"])
def test_a_failure_after_start_upload_keeps_the_asset_and_logs_its_id(entry, capsys, failing_step):
    from miris_uploader import MirisError

    client = _client()
    write_metadata = MagicMock()
    put = MagicMock()
    error = MirisError(f"{failing_step} failed")
    if failing_step == "put_artifact":
        put.side_effect = error
    elif failing_step == "write_metadata":
        write_metadata.side_effect = error
    else:
        getattr(client, failing_step).side_effect = error

    with patch.object(entry, "_put_artifact", put), pytest.raises(MirisError):
        _run(entry, client, write_metadata)

    assert _kept_asset_logs(capsys.readouterr().out) == [
        {"msg": "miris_asset_kept", "asset_uuid": UUID, "error": f"{failing_step} failed"}]


def test_a_start_upload_failure_has_no_asset_to_keep(entry, capsys):
    from miris_uploader import MirisError

    client = _client()
    client.start_upload.side_effect = MirisError("POST /v1/content failed [500]")

    with pytest.raises(MirisError):
        _run(entry, client)
    assert _kept_asset_logs(capsys.readouterr().out) == []


def test_a_generate_failure_does_not_fail_the_upload(entry):
    client = _client()
    client.trigger_generate_best_effort.return_value = None
    write_metadata = MagicMock()

    with patch.object(entry, "_put_artifact"):
        assert _run(entry, client, write_metadata) == UUID
    write_metadata.assert_called_once_with(UUID)


def test_a_terminal_error_state_exits_8(entry):
    from miris_uploader import MirisError

    client = _client()
    client.poll_until_terminal.side_effect = MirisError("asset reached terminal error state 'failed'")

    with patch.object(entry, "_put_artifact"), pytest.raises(SystemExit) as exited:
        entry.upload_or_exit(client, **_upload_args())

    assert exited.value.code == entry.EXIT_MIRIS_FAILED == 8


def test_a_processing_timeout_exits_9_without_writing_the_stream_metadata(entry):
    client = _client()
    client.poll_until_terminal.side_effect = TimeoutError("still processing")
    write_metadata = MagicMock()

    with patch.object(entry, "_put_artifact"), pytest.raises(SystemExit) as exited:
        entry.upload_or_exit(client, **_upload_args(write_metadata))

    assert exited.value.code == entry.EXIT_MIRIS_TIMEOUT == 9
    write_metadata.assert_not_called()


def _miris_api(asset_state):
    """Miris over HTTP: the asset is created, and every poll reports `asset_state`. A DELETE of the
    asset is answered too, so a stray one is recorded as a call rather than failing on its own."""
    responses.add(responses.POST, f"{BASE}/v1/content", json=_start_response(), status=200)
    responses.add(responses.PUT, f"{BASE}/v1/content/{UUID}", json={"id": UUID}, status=200)
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"id": UUID, "state": asset_state},
                  status=200)
    responses.add(responses.POST, f"{BASE}/v1/asset/{UUID}/generate", json={}, status=200)
    responses.add(responses.DELETE, f"{BASE}/v1/asset/{UUID}", status=204)


@responses.activate
@pytest.mark.parametrize("failure,asset_state,expected", [
    ("processing_timeout", "processing_preview", 9),
    ("terminal_error", "failed", 8),
    ("s3_put", "preview", RuntimeError),
    ("metadata_write", "preview", RuntimeError),
])
def test_no_failure_sends_a_delete_to_miris(entry, capsys, failure, asset_state, expected):
    from miris_uploader import MirisClient

    _miris_api(asset_state)
    client = MirisClient(BASE, "key", sleep=lambda _seconds: None)
    put = MagicMock(side_effect=RuntimeError("S3 PUT failed") if failure == "s3_put" else None)
    write_metadata = MagicMock(
        side_effect=RuntimeError("metadata write failed") if failure == "metadata_write" else None)
    args = _upload_args(write_metadata, task_timeout=0)

    with patch.object(entry, "_put_artifact", put):
        if isinstance(expected, int):
            with pytest.raises(SystemExit) as exited:
                entry.upload_or_exit(client, **args)
            assert exited.value.code == expected
        else:
            with pytest.raises(expected):
                entry.upload_or_exit(client, **args)

    methods = [call.request.method for call in responses.calls]
    # Control: the run reached Miris, so the absence below is not vacuous.
    assert methods[0] == "POST"
    assert "DELETE" not in methods, methods
    kept = _kept_asset_logs(capsys.readouterr().out)
    assert [line["asset_uuid"] for line in kept] == [UUID]
    if failure != "metadata_write":
        write_metadata.assert_not_called()


def _module_int_constants(path, prefix):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith(prefix)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)
    }


def _end_lambda_exit_codes():
    path = os.path.join(_CONTAINER, "..", "lambda", "pipelineEnd.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "EXIT_CODE_REASONS":
            return {key.value for key in node.value.keys}
    raise AssertionError("EXIT_CODE_REASONS not found in pipelineEnd.py")


def test_every_exit_code_has_a_user_facing_reason_in_the_end_lambda():
    """The container and the lambda are separate code assets, so the table is kept in step here."""
    codes = _module_int_constants(os.path.join(_CONTAINER, "__main__.py"), "EXIT_")
    # Control: the parse found the constants, or the subset check below is vacuous.
    assert len(codes) >= 8
    assert len(set(codes.values())) == len(codes), "two exit codes share a value"
    reasons = _end_lambda_exit_codes()
    assert set(codes.values()) | {1} <= reasons, sorted(set(codes.values()) - reasons)
