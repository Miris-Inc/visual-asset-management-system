#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""The container's upload flow: a failure after the Miris asset exists deletes it, and every way the
flow can end maps to an exit code the pipelineEnd lambda turns into a user-facing reason."""
import ast
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_CONTAINER = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _CONTAINER)

UUID = "11111111-2222-3333-4444-555555555555"


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


def _client():
    client = MagicMock()
    client.start_upload.return_value = {
        "id": UUID,
        "upload_path": "s3://miris-uploads/ws/up",
        "endpoint_url": "https://s3.example.test",
        "region": "us-west-1",
        "access_key_id": "AK",
        "secret_key": "SK",
        "session_token": "ST",
    }
    client.poll_until_terminal.return_value = {"state": "preview"}
    client.trigger_generate_best_effort.return_value = {"state": "processing_streamable"}
    client.delete_asset.return_value = True
    return client


def _run(entry, client, write_metadata=None):
    return entry.run_miris_upload(
        client,
        name="Pump",
        tags=["vams"],
        upload_local="/workdir/pump.usdz",
        upload_filename="pump.usdz",
        content_type="model/vnd.usdz+zip",
        size=10,
        task_timeout=60,
        write_metadata=write_metadata or MagicMock(),
    )


def test_a_successful_upload_keeps_the_asset(entry):
    client = _client()
    write_metadata = MagicMock()
    with patch.object(entry, "_put_artifact"):
        assert _run(entry, client, write_metadata) == UUID

    write_metadata.assert_called_once_with(UUID)
    client.delete_asset.assert_not_called()


@pytest.mark.parametrize("failing_step", [
    "put_artifact", "mark_upload_complete", "poll_until_terminal", "write_metadata"])
def test_a_failure_after_start_upload_deletes_the_asset_and_logs_its_id(entry, capsys, failing_step):
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

    client.delete_asset.assert_called_once_with(UUID)
    logged = capsys.readouterr().out
    assert '"upload_abandoned"' in logged and UUID in logged


def test_a_failed_delete_still_surfaces_the_original_error(entry, capsys):
    client = _client()
    client.delete_asset.return_value = False
    client.poll_until_terminal.side_effect = TimeoutError("still processing")

    with patch.object(entry, "_put_artifact"), pytest.raises(TimeoutError):
        _run(entry, client)

    assert '"deleted": false' in capsys.readouterr().out


def test_a_start_upload_failure_has_no_asset_to_delete(entry):
    from miris_uploader import MirisError

    client = _client()
    client.start_upload.side_effect = MirisError("POST /v1/content failed [500]")

    with pytest.raises(MirisError):
        _run(entry, client)
    client.delete_asset.assert_not_called()


def test_a_generate_failure_does_not_fail_the_upload(entry):
    client = _client()
    client.trigger_generate_best_effort.return_value = None
    write_metadata = MagicMock()

    with patch.object(entry, "_put_artifact"):
        assert _run(entry, client, write_metadata) == UUID
    write_metadata.assert_called_once_with(UUID)
    client.delete_asset.assert_not_called()


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


def test_a_raising_cleanup_still_logs_the_abandoned_asset_and_exits_8(entry, capsys):
    from miris_uploader import MirisError

    client = _client()
    client.poll_until_terminal.side_effect = MirisError("asset reached terminal error state 'failed'")
    client.delete_asset.side_effect = RuntimeError("cleanup blew up")

    with patch.object(entry, "_put_artifact"), pytest.raises(SystemExit) as exited:
        entry.upload_or_exit(
            client, name="Pump", tags=["vams"], upload_local="/workdir/pump.usdz",
            upload_filename="pump.usdz", content_type="model/vnd.usdz+zip", size=10,
            task_timeout=60, write_metadata=MagicMock())

    assert exited.value.code == entry.EXIT_MIRIS_FAILED == 8
    logged = capsys.readouterr().out
    assert '"upload_abandoned"' in logged and UUID in logged and '"deleted": false' in logged


def test_a_processing_timeout_exits_9(entry):
    client = _client()
    client.poll_until_terminal.side_effect = TimeoutError("still processing")

    with patch.object(entry, "_put_artifact"), pytest.raises(SystemExit) as exited:
        entry.upload_or_exit(
            client, name="Pump", tags=["vams"], upload_local="/workdir/pump.usdz",
            upload_filename="pump.usdz", content_type="model/vnd.usdz+zip", size=10,
            task_timeout=60, write_metadata=MagicMock())

    assert exited.value.code == entry.EXIT_MIRIS_TIMEOUT == 9
    client.delete_asset.assert_called_once_with(UUID)
