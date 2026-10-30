# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""GET /addon/miris/asset-status?databaseId=...&assetId=...&filePath=...

The Miris asset UUID comes only from the file's own metadata row, Tier-2 GET on the asset is
enforced before any metadata read, the binding tags are checked in both viewer key modes, and every
outcome maps onto the documented state set.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.backend.handlers.addon.miris import getAssetStatus, mirisIntegration

from .conftest import FakeLambdaContext, claims_after_normalizing, rest_event as _rest_event

DATABASE_ID = "db-1"
ASSET_ID = "asset-1"
STORED_UUID = "8d65af7c-5412-404b-a494-fbd2cdb62442"
OTHER_UUID = "bc336bab-e3e4-4c50-bd00-9d6cbbd9f194"
ROUTE_PATH = "/addon/miris/asset-status"
BINDING_TAG = f"vams-asset-{ASSET_ID}"
DATABASE_TAG = f"vams-database-{DATABASE_ID}"
RESPONSE_BODY = "miris-response-body-must-not-surface"
SORT_KEY_FIELD = "databaseId:assetId:filePath"
INTEGRATION_KEY = "status-integration-key-do-not-log-41c2"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf"


# A stand-in for request_to_claims, which also normalizes the REST event the handler is given.
_AS_USER = claims_after_normalizing({"tokens": ["user"], "roles": []})


def _event(file_path="/scene.usd", method="GET", path=ROUTE_PATH, extra_query=None,
           database_id=DATABASE_ID, asset_id=ASSET_ID):
    query = {"databaseId": database_id, "assetId": asset_id}
    if file_path is not None:
        query["filePath"] = file_path
    query.update(extra_query or {})
    return _rest_event(method, path, query={k: v for k, v in query.items() if v is not None})


def _metadata_table(rows_by_file=None):
    """Fake file-metadata table: {sortKey: {metadataKey: value}} answered by get_item."""
    rows_by_file = rows_by_file or {}
    table = MagicMock()

    def get_item(Key):
        value = rows_by_file.get(Key[SORT_KEY_FIELD], {}).get(Key["metadataKey"])
        if value is None:
            return {}
        return {
            "Item": {
                "metadataKey": Key["metadataKey"],
                SORT_KEY_FIELD: Key[SORT_KEY_FIELD],
                "metadataValue": value,
                "metadataValueType": "string",
            }
        }

    table.get_item.side_effect = get_item
    return table


def _asset_table(found=True, root_key=None):
    table = MagicMock()
    item = {"databaseId": DATABASE_ID, "assetId": ASSET_ID, "bucketId": "bucket-1"}
    if root_key is not None:
        item["assetLocation"] = {"Key": root_key}
    table.get_item.return_value = {"Item": item} if found else {}
    return table


def _enforcer(api=True, entity=True):
    enforcer = MagicMock()
    enforcer.enforceAPI.return_value = api
    enforcer.enforce.return_value = entity
    return enforcer


def _miris_response(status_code=200, body=None, text=None):
    response = MagicMock()
    response.status_code = status_code
    response.text = text if text is not None else json.dumps(body or {})
    if body is None and text is not None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body or {}
    return response


def _stream_row(uuid=STORED_UUID, **overrides):
    row = {
        "miris_asset_uuid": uuid,
        "miris_source_version_id": "v-abc",
        "miris_uploaded_at": "2026-10-14T12:00:00Z",
        "miris_display_name": "Scene",
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not None}


def _bound_asset(tags=(BINDING_TAG, DATABASE_TAG), state="streamable"):
    """The Integration API's answer for a Miris asset carrying `tags`."""
    return _miris_response(body={"id": STORED_UUID, "state": state, "tags": list(tags)})


def _invoke(event, metadata_table=None, asset_table=None, enforcer=None, miris_get=None,
            integration_get=None, viewer_key="viewer-key-0000000000",
            base_url="https://app.miris.com", viewer_api=True, context=None, mode="static"):
    """Invoke the handler. With `viewer_api`, the shared session answers viewer API requests with
    `miris_get(url, **kwargs)` and Integration API requests with `integration_get(url, **kwargs)`
    (by default a bound asset), and Secrets Manager serves `INTEGRATION_KEY`; otherwise the
    `miris_api` fixture's session and secret stay in place."""
    metadata_table = metadata_table if metadata_table is not None else _metadata_table()
    asset_table = asset_table if asset_table is not None else _asset_table()
    enforcer = enforcer if enforcer is not None else _enforcer()
    miris_get = miris_get if miris_get is not None else MagicMock(
        return_value=_miris_response(body={"state": "streamable"})
    )
    integration_get = integration_get if integration_get is not None else MagicMock(
        return_value=_bound_asset()
    )

    def request(method, url, **kwargs):
        if "/viewer/v1/" in url:
            return miris_get(url, **kwargs)
        return integration_get(url, **kwargs)

    session = SimpleNamespace(request=request)
    secretsmanager = MagicMock()
    secretsmanager.get_secret_value.return_value = {"SecretString": INTEGRATION_KEY}
    with patch.object(getAssetStatus, "asset_file_metadata_table", metadata_table), \
         patch.object(getAssetStatus, "asset_table", asset_table), \
         patch.object(getAssetStatus, "CasbinEnforcer", return_value=enforcer), \
         patch.object(getAssetStatus, "request_to_claims", side_effect=_AS_USER), \
         patch.object(getAssetStatus, "miris_viewer_key", viewer_key), \
         patch.object(getAssetStatus, "miris_viewer_key_mode", mode):
        if viewer_api:
            mirisIntegration._invalidate_api_key_cache()
            try:
                with patch.object(mirisIntegration, "MIRIS_API_BASE_URL", base_url), \
                     patch.object(mirisIntegration, "MIRIS_API_KEY_SECRET_ARN", SECRET_ARN), \
                     patch.object(mirisIntegration, "_get_secretsmanager_client", return_value=secretsmanager), \
                     patch.object(mirisIntegration, "_get_session", return_value=session):
                    response = getAssetStatus.lambda_handler(event, context or FakeLambdaContext())
            finally:
                mirisIntegration._invalidate_api_key_cache()
        else:
            response = getAssetStatus.lambda_handler(event, context or FakeLambdaContext())
    body = json.loads(response["body"])
    return response["statusCode"], body, metadata_table, miris_get


@pytest.mark.unit
class TestResponseShapeAndStates:

    def test_streamable_returns_all_four_keys_and_the_miris_state(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        status, body, _, miris_get = _invoke(_event(), metadata_table=table)

        assert status == 200
        assert body == {
            "filePath": "/scene.usd",
            "mirisAssetUuid": STORED_UUID,
            "sourceVersionId": "v-abc",
            "uploadedAt": "2026-10-14T12:00:00Z",
            "displayName": "Scene",
            "state": "streamable",
        }
        miris_get.assert_called_once()
        args, kwargs = miris_get.call_args
        assert args[0] == f"https://app.miris.com/viewer/v1/asset/{STORED_UUID}"
        assert kwargs["headers"]["Miris-Viewer-Key"] == "viewer-key-0000000000"
        assert "Miris-API-Key" not in kwargs["headers"]
        assert sum(kwargs["timeout"]) <= mirisIntegration._REQUEST_BUDGET_SECONDS

    @pytest.mark.parametrize("miris_state", ["preview", "error", "failed"])
    def test_other_miris_states_are_passed_through(self, miris_state):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        miris_get = MagicMock(return_value=_miris_response(body={"state": miris_state}))
        status, body, _, _ = _invoke(_event(), metadata_table=table, miris_get=miris_get)
        assert status == 200
        assert body["state"] == miris_state
        assert body["mirisAssetUuid"] == STORED_UUID

    def test_no_stream_keys_is_not_streamed_without_calling_miris(self):
        status, body, _, miris_get = _invoke(_event())
        assert status == 200
        assert body == {
            "filePath": "/scene.usd",
            "mirisAssetUuid": None,
            "sourceVersionId": None,
            "uploadedAt": None,
            "displayName": None,
            "state": "notStreamed",
        }
        miris_get.assert_not_called()

    @pytest.mark.parametrize("stored", ["", "   "])
    def test_empty_uuid_is_not_streamed(self, stored):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row(uuid=stored)})
        status, body, _, miris_get = _invoke(_event(), metadata_table=table)
        assert status == 200
        assert body["state"] == "notStreamed"
        assert body["mirisAssetUuid"] is None
        miris_get.assert_not_called()

    def test_non_uuid_value_is_invalid_uuid_without_calling_miris(self):
        table = _metadata_table(
            {f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row(uuid="not-a-uuid")}
        )
        status, body, _, miris_get = _invoke(_event(), metadata_table=table)
        assert status == 200
        assert body["state"] == "invalidUuid"
        assert body["mirisAssetUuid"] == "not-a-uuid"
        miris_get.assert_not_called()

    def test_empty_source_version_is_preserved_and_missing_keys_are_null(self):
        row = _stream_row(miris_source_version_id="", miris_uploaded_at=None, miris_display_name=None)
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": row})
        status, body, _, _ = _invoke(_event(), metadata_table=table)
        assert status == 200
        assert body["sourceVersionId"] == ""
        assert body["uploadedAt"] is None
        assert body["displayName"] is None

    @pytest.mark.parametrize("viewer_key, base_url", [("", "https://app.miris.com"), ("viewer-key-0000000000", "")])
    def test_no_credential_is_unknown_without_calling_miris(self, viewer_key, base_url):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_bound_asset())
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, viewer_key=viewer_key, base_url=base_url,
            integration_get=integration_get,
        )
        assert status == 200
        assert body["state"] == "unknown"
        assert body["mirisAssetUuid"] == STORED_UUID
        miris_get.assert_not_called()
        integration_get.assert_not_called()

    @pytest.mark.parametrize("mode", ["", "Static", "per-view"])
    def test_an_unrecognized_mode_is_unknown_without_calling_miris(self, mode):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_bound_asset())
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, integration_get=integration_get, mode=mode
        )
        assert status == 200
        assert body["state"] == "unknown"
        miris_get.assert_not_called()
        integration_get.assert_not_called()


@pytest.mark.unit
class TestMirisBadRequestIsProcessing:
    """The viewer API answers 400 while Miris is still preparing a fresh asset."""

    def test_400_is_processing_and_the_uuid_is_returned(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        miris_get = MagicMock(
            return_value=_miris_response(
                status_code=400, body={"type": "about:blank", "status": 400, "title": "Invalid Request"}
            )
        )
        status, body, _, _ = _invoke(_event(), metadata_table=table, miris_get=miris_get)
        assert status == 200
        assert body["state"] == "processing"
        assert body["mirisAssetUuid"] == STORED_UUID


@pytest.mark.unit
class TestMirisFailuresAreUnknown:
    """Anything short of a definitive processing failure is `unknown`, and the UUID is still returned."""

    @pytest.mark.parametrize(
        "miris_get",
        [
            MagicMock(return_value=_miris_response(status_code=404, body={"message": "not found"})),
            MagicMock(return_value=_miris_response(status_code=401, body={"message": "unauthorized"})),
            MagicMock(return_value=_miris_response(status_code=403, body={"message": "forbidden"})),
            MagicMock(return_value=_miris_response(status_code=500, body={"message": "boom"})),
            MagicMock(return_value=_miris_response(status_code=200, text="<html>")),
            MagicMock(return_value=_miris_response(status_code=200, body={"name": "no state"})),
            MagicMock(return_value=_miris_response(status_code=200, body={"state": "x" * 200})),
            MagicMock(side_effect=requests.ConnectionError("unreachable")),
        ],
        ids=["404", "401", "403", "500", "non-json", "no-state", "malformed-state", "connection-error"],
    )
    def test_indeterminate_miris_outcomes(self, miris_get):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        status, body, _, _ = _invoke(_event(), metadata_table=table, miris_get=miris_get)
        assert status == 200
        assert body["state"] == "unknown"
        assert body["mirisAssetUuid"] == STORED_UUID


@pytest.mark.unit
class TestViewerProbe:

    def test_no_response_body_reaches_the_logs(self, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        for miris_get in (
            MagicMock(return_value=_miris_response(status_code=500, text=RESPONSE_BODY)),
            MagicMock(return_value=_miris_response(status_code=200, text=RESPONSE_BODY)),
            MagicMock(side_effect=requests.ConnectionError(RESPONSE_BODY)),
        ):
            status, body, _, _ = _invoke(_event(), metadata_table=table, miris_get=miris_get)
            assert status == 200
            assert body["state"] == "unknown"

        # Control: each failure was logged.
        assert "HTTP 500" in log_recorder.text()
        assert "no state" in log_recorder.text()
        assert "ConnectionError" in log_recorder.text()
        for text in (log_recorder.text(), caplog.text):
            assert RESPONSE_BODY not in text
            assert "viewer-key-0000000000" not in text
            assert INTEGRATION_KEY not in text

    def test_too_little_time_left_is_unknown_without_a_request(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_bound_asset())
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, integration_get=integration_get,
            context=FakeLambdaContext(remaining_ms=2000),
        )
        assert status == 200
        assert body["state"] == "unknown"
        assert body["mirisAssetUuid"] == STORED_UUID
        miris_get.assert_not_called()
        integration_get.assert_not_called()


@pytest.mark.unit
class TestStaticModeChecksTheBinding:
    """In static mode the asset's tags are read with the Integration Key before the viewer probe."""

    def test_the_binding_is_read_with_the_integration_key_before_the_viewer_probe(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        order = []
        integration_get = MagicMock(side_effect=lambda url, **kw: order.append("integration") or _bound_asset())
        miris_get = MagicMock(
            side_effect=lambda url, **kw: order.append("viewer") or _miris_response(body={"state": "streamable"})
        )
        status, body, _, _ = _invoke(
            _event(), metadata_table=table, integration_get=integration_get, miris_get=miris_get
        )
        assert status == 200
        assert body["state"] == "streamable"
        assert order == ["integration", "viewer"]
        args, kwargs = integration_get.call_args
        assert args[0] == f"https://app.miris.com/v1/asset/{STORED_UUID}"
        assert kwargs["headers"]["Miris-API-Key"] == INTEGRATION_KEY
        assert "Miris-Viewer-Key" not in kwargs["headers"]
        assert kwargs["allow_redirects"] is False

    @pytest.mark.parametrize(
        "tags",
        [
            [],
            ["vams"],
            [BINDING_TAG],
            [DATABASE_TAG],
            ["vams", "vams-asset-asset-2", DATABASE_TAG],
            ["vams", BINDING_TAG, "vams-database-db-2"],
            [f"{BINDING_TAG}-x", DATABASE_TAG],
        ],
        ids=["empty", "vams-only", "asset-tag-only", "database-tag-only", "another-asset",
             "same-asset-id-other-database", "prefix-only"],
    )
    def test_an_unbound_asset_is_not_bound_without_the_viewer_probe(self, tags):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_bound_asset(tags=tags))
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, integration_get=integration_get
        )
        assert status == 200
        assert body["state"] == "notBound"
        assert body["mirisAssetUuid"] == STORED_UUID
        miris_get.assert_not_called()

    def test_a_missing_miris_asset_is_not_bound_without_the_viewer_probe(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_miris_response(status_code=404, body={"message": "nf"}))
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, integration_get=integration_get
        )
        assert status == 200
        assert body["state"] == "notBound"
        miris_get.assert_not_called()

    @pytest.mark.parametrize(
        "integration_get",
        [
            MagicMock(return_value=_miris_response(status_code=500, body={"message": "boom"})),
            MagicMock(return_value=_miris_response(status_code=302, body={})),
            MagicMock(return_value=_miris_response(status_code=200, text="<html>")),
            MagicMock(side_effect=requests.ConnectionError("unreachable")),
        ],
        ids=["500", "redirect", "non-json", "connection-error"],
    )
    def test_an_unreadable_binding_is_unknown_without_the_viewer_probe(self, integration_get):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        status, body, _, miris_get = _invoke(
            _event(), metadata_table=table, integration_get=integration_get
        )
        assert status == 200
        assert body["state"] == "unknown"
        assert body["mirisAssetUuid"] == STORED_UUID
        miris_get.assert_not_called()

    def test_the_viewer_state_wins_over_the_integration_state(self):
        """The viewer probe is what the frontend's key can see, so its state is the one reported."""
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        integration_get = MagicMock(return_value=_bound_asset(state="streamable"))
        miris_get = MagicMock(return_value=_miris_response(status_code=404, body={"message": "nf"}))
        _, body, _, _ = _invoke(
            _event(), metadata_table=table, integration_get=integration_get, miris_get=miris_get
        )
        assert body["state"] == "unknown"


@pytest.mark.unit
class TestUuidComesOnlyFromTheFile:

    def test_reads_exactly_the_four_keys_for_exactly_this_file(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        _invoke(_event(), metadata_table=table)
        keys = [call.kwargs["Key"] for call in table.get_item.call_args_list]
        assert keys == [
            {"metadataKey": k, SORT_KEY_FIELD: f"{DATABASE_ID}:{ASSET_ID}:/scene.usd"}
            for k in (
                "miris_asset_uuid",
                "miris_source_version_id",
                "miris_uploaded_at",
                "miris_display_name",
            )
        ]

    def test_a_client_supplied_uuid_is_ignored(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        event = _event(extra_query={"mirisAssetUuid": OTHER_UUID})
        event["pathParameters"] = {"mirisAssetUuid": OTHER_UUID}
        integration_get = MagicMock(return_value=_bound_asset())
        status, body, _, miris_get = _invoke(event, metadata_table=table, integration_get=integration_get)
        assert status == 200
        assert body["mirisAssetUuid"] == STORED_UUID
        assert OTHER_UUID not in miris_get.call_args.args[0]
        assert OTHER_UUID not in integration_get.call_args.args[0]

    def test_a_stream_on_another_file_is_not_returned(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/other.usd": _stream_row()})
        status, body, _, miris_get = _invoke(_event("/scene.usd"), metadata_table=table)
        assert status == 200
        assert body["state"] == "notStreamed"
        miris_get.assert_not_called()

    def test_a_path_below_the_route_is_not_dispatched(self):
        event = _event(path=f"{ROUTE_PATH}/{OTHER_UUID}")
        integration_get = MagicMock(return_value=_bound_asset())
        status, _, table, miris_get = _invoke(event, integration_get=integration_get)
        assert status == 400
        table.get_item.assert_not_called()
        miris_get.assert_not_called()
        integration_get.assert_not_called()


@pytest.mark.unit
class TestFilePathNormalization:

    @pytest.mark.parametrize("given", ["scene.usd", "/scene.usd", f"/{ASSET_ID}/scene.usd", f"{ASSET_ID}/scene.usd"])
    def test_equivalent_forms_address_the_same_row(self, given):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        status, body, _, _ = _invoke(_event(given), metadata_table=table)
        assert status == 200
        assert body["filePath"] == "/scene.usd"
        assert body["mirisAssetUuid"] == STORED_UUID

    def test_nested_paths_keep_their_folders(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/a/b/scene.usda": _stream_row()})
        status, body, _, _ = _invoke(_event("a/b/scene.usda"), metadata_table=table)
        assert status == 200
        assert body["filePath"] == "/a/b/scene.usda"
        assert body["state"] == "streamable"

    @pytest.mark.parametrize(
        "file_path",
        [None, "", "/", "/../secret.usd", "/a/../b.usd", "/folder/", "/" + "a" * 1100, "/a\nb.usd"],
        ids=["missing", "empty", "root", "traversal", "inner-traversal", "folder", "too-long", "newline"],
    )
    def test_invalid_file_path_is_400_without_any_read(self, file_path):
        asset_table = _asset_table()
        status, _, table, miris_get = _invoke(_event(file_path), asset_table=asset_table)
        assert status == 400
        asset_table.get_item.assert_not_called()
        table.get_item.assert_not_called()
        miris_get.assert_not_called()

    @pytest.mark.parametrize(
        "database_id, asset_id",
        [(None, ASSET_ID), (DATABASE_ID, None), ("", ASSET_ID), (DATABASE_ID, ""),
         ("db 1", ASSET_ID), ("d" * 300, ASSET_ID), (DATABASE_ID, "../asset-2"), (DATABASE_ID, "a" * 300)],
        ids=["no-database", "no-asset", "empty-database", "empty-asset", "database-space",
             "database-too-long", "asset-traversal", "asset-too-long"],
    )
    def test_invalid_ids_are_400_without_any_read(self, database_id, asset_id):
        asset_table = _asset_table()
        status, _, table, miris_get = _invoke(
            _event(database_id=database_id, asset_id=asset_id), asset_table=asset_table
        )
        assert status == 400
        asset_table.get_item.assert_not_called()
        table.get_item.assert_not_called()
        miris_get.assert_not_called()

    def test_ids_in_path_parameters_are_not_read(self):
        """The asset is named by the query string only; path parameters carry nothing."""
        asset_table = _asset_table()
        event = _event(database_id=None, asset_id=None)
        event["pathParameters"] = {"databaseId": DATABASE_ID, "assetId": ASSET_ID}
        status, _, _, _ = _invoke(event, asset_table=asset_table)
        assert status == 400
        asset_table.get_item.assert_not_called()


# (asset root key, file's full object key, relative path the metadata is stored under)
ASSET_LAYOUTS = {
    "default-bucket": (f"{ASSET_ID}/", f"{ASSET_ID}/folder/scene.usd", "/folder/scene.usd"),
    "base-assets-prefix": (
        f"assets/{ASSET_ID}/",
        f"assets/{ASSET_ID}/folder/scene.usd",
        "/folder/scene.usd",
    ),
    "bucket-existing-key": ("projects/site-a/", "projects/site-a/sub/scene.usd", "/sub/scene.usd"),
    "root-without-slashes": ("projects/site-a", "projects/site-a/sub/scene.usd", "/sub/scene.usd"),
    "root-with-leading-slash": ("/projects/site-a/", "projects/site-a/sub/scene.usd", "/sub/scene.usd"),
}


@pytest.mark.unit
class TestAssetRootKey:
    """filePath may be the file's full object key under any asset root layout."""

    @pytest.mark.parametrize("layout", sorted(ASSET_LAYOUTS))
    @pytest.mark.parametrize("form", ["key", "slash-key", "relative", "relative-no-slash"])
    def test_every_form_addresses_the_stored_row(self, layout, form):
        root_key, full_key, relative = ASSET_LAYOUTS[layout]
        given = {
            "key": full_key,
            "slash-key": "/" + full_key,
            "relative": relative,
            "relative-no-slash": relative.lstrip("/"),
        }[form]
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:{relative}": _stream_row()})
        status, body, _, _ = _invoke(
            _event(given), metadata_table=table, asset_table=_asset_table(root_key=root_key)
        )
        assert status == 200
        assert body["filePath"] == relative
        assert body["mirisAssetUuid"] == STORED_UUID

    @pytest.mark.parametrize(
        "root_key, given",
        [
            (f"assets/{ASSET_ID}/", "/assets/asset-2/scene.usd"),
            (f"{ASSET_ID}/", "/asset-2/scene.usd"),
            ("projects/site-a/", "/projects/site-b/scene.usd"),
        ],
        ids=["other-asset-under-base-prefix", "other-asset-default", "sibling-existing-key"],
    )
    def test_another_assets_key_is_not_streamed(self, root_key, given):
        # The other asset's row exists; only this asset's rows are ever addressed.
        table = _metadata_table({
            f"{DATABASE_ID}:asset-2:/scene.usd": _stream_row(),
            f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row(uuid=OTHER_UUID),
        })
        status, body, _, miris_get = _invoke(
            _event(given), metadata_table=table, asset_table=_asset_table(root_key=root_key)
        )
        assert status == 200
        assert body["state"] == "notStreamed"
        assert body["mirisAssetUuid"] is None
        sort_keys = {c.kwargs["Key"][SORT_KEY_FIELD] for c in table.get_item.call_args_list}
        assert sort_keys == {f"{DATABASE_ID}:{ASSET_ID}:{given}"}
        miris_get.assert_not_called()

    def test_traversal_in_a_full_key_is_400(self):
        asset_table = _asset_table(root_key=f"assets/{ASSET_ID}/")
        status, _, table, _ = _invoke(
            _event(f"/assets/{ASSET_ID}/../asset-2/scene.usd"), asset_table=asset_table
        )
        assert status == 400
        table.get_item.assert_not_called()

    def test_the_root_itself_is_a_folder_and_400(self):
        status, _, table, _ = _invoke(
            _event("/projects/site-a/"), asset_table=_asset_table(root_key="projects/site-a/")
        )
        assert status == 400
        table.get_item.assert_not_called()


@pytest.mark.unit
class TestAuthorization:

    def test_tier2_denial_is_403_and_reads_no_metadata(self):
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        status, _, _, miris_get = _invoke(
            _event(), metadata_table=table, enforcer=_enforcer(api=True, entity=False)
        )
        assert status == 403
        table.get_item.assert_not_called()
        miris_get.assert_not_called()

    def test_tier2_is_checked_with_get_on_the_asset(self):
        enforcer = _enforcer()
        _invoke(_event(), enforcer=enforcer)
        asset_arg, action = enforcer.enforce.call_args.args
        assert action == "GET"
        assert asset_arg["object__type"] == "asset"
        assert asset_arg["assetId"] == ASSET_ID

    def test_tier1_denial_is_403(self):
        asset_table = _asset_table()
        status, _, table, miris_get = _invoke(
            _event(), asset_table=asset_table, enforcer=_enforcer(api=False)
        )
        assert status == 403
        asset_table.get_item.assert_not_called()
        table.get_item.assert_not_called()
        miris_get.assert_not_called()

    def test_missing_asset_is_an_error_and_reads_no_metadata(self):
        status, body, table, miris_get = _invoke(_event(), asset_table=_asset_table(found=False))
        assert status == 400
        assert body["message"] == "Asset not found"
        table.get_item.assert_not_called()
        miris_get.assert_not_called()

    def test_non_get_is_rejected(self):
        status, _, table, _ = _invoke(_event(method="POST"))
        assert status == 400
        table.get_item.assert_not_called()


@pytest.mark.unit
class TestPerViewModeUsesTheIntegrationApi:
    """In perView mode no deployment-wide viewer key exists, so the state comes from the
    Integration API (`GET /v1/asset/{uuid}` with the Integration Key)."""

    def _invoke_per_view(self, miris_api, metadata_table=None, context=None):
        table = metadata_table if metadata_table is not None else _metadata_table(
            {f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()}
        )
        viewer_get = MagicMock()
        status, body, _, _ = _invoke(
            _event(), metadata_table=table, miris_get=viewer_get, viewer_key="",
            viewer_api=False, context=context, mode="perView",
        )
        return status, body, viewer_get

    def test_the_state_is_read_with_the_integration_key(self, miris_api):
        miris_api.responses.append((200, {"id": STORED_UUID, "state": "streamable", "tags": ["vams", BINDING_TAG, DATABASE_TAG]}))
        status, body, viewer_get = self._invoke_per_view(miris_api)

        assert status == 200
        assert body["state"] == "streamable"
        assert body["mirisAssetUuid"] == STORED_UUID
        assert len(miris_api.calls) == 1
        call = miris_api.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{miris_api.base_url}/v1/asset/{STORED_UUID}"
        assert call["headers"]["Miris-API-Key"] == miris_api.integration_key
        viewer_get.assert_not_called()

    @pytest.mark.parametrize("miris_state", ["preview", "processing", "error", "failed"])
    def test_other_states_are_passed_through(self, miris_api, miris_state):
        miris_api.responses.append((200, {"state": miris_state, "tags": [BINDING_TAG, DATABASE_TAG]}))
        _, body, _ = self._invoke_per_view(miris_api)
        assert body["state"] == miris_state

    @pytest.mark.parametrize(
        "outcome",
        [
            (500, "boom"),
            (400, "bad"),
            (200, {"state": "x" * 200, "tags": [BINDING_TAG, DATABASE_TAG]}),
            (200, {"name": "none", "tags": [BINDING_TAG, DATABASE_TAG]}),
            (200, "<html>"),
        ],
        ids=["500", "400", "malformed-state", "no-state", "non-json"],
    )
    def test_indeterminate_outcomes_are_unknown(self, miris_api, outcome):
        miris_api.responses.append(outcome)
        status, body, _ = self._invoke_per_view(miris_api)
        assert status == 200
        assert body["state"] == "unknown"
        assert body["mirisAssetUuid"] == STORED_UUID

    @pytest.mark.parametrize(
        "tags",
        [
            [],
            None,
            ["vams"],
            [BINDING_TAG],
            [DATABASE_TAG],
            ["vams", "vams-asset-asset-2", DATABASE_TAG],
            ["vams", BINDING_TAG, "vams-database-db-2"],
            [f"{BINDING_TAG}-x", DATABASE_TAG],
        ],
        ids=["empty", "null", "vams-only", "asset-tag-only", "database-tag-only", "another-asset",
             "same-asset-id-other-database", "prefix-only"],
    )
    def test_an_unbound_asset_is_not_bound(self, miris_api, tags):
        miris_api.responses.append((200, {"state": "streamable", "tags": tags}))
        status, body, _ = self._invoke_per_view(miris_api)
        assert status == 200
        assert body["state"] == "notBound"
        assert body["mirisAssetUuid"] == STORED_UUID

    def test_a_missing_miris_asset_is_not_bound(self, miris_api):
        miris_api.responses.append(404)
        status, body, _ = self._invoke_per_view(miris_api)
        assert status == 200
        assert body["state"] == "notBound"
        assert body["mirisAssetUuid"] == STORED_UUID

    def test_a_request_received_long_ago_is_unknown_without_a_request(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        event = _event()
        event["requestContext"]["requestTimeEpoch"] = int((5000.0 - 27) * 1000)
        status, body, _, _ = _invoke(
            event, metadata_table=table, viewer_key="", viewer_api=False, mode="perView"
        )
        assert status == 200
        assert body["state"] == "unknown"
        assert miris_api.calls == []

    def test_too_little_time_left_is_unknown_without_a_request(self, miris_api):
        _, body, _ = self._invoke_per_view(miris_api, context=FakeLambdaContext(remaining_ms=2000))
        assert body["state"] == "unknown"
        assert miris_api.calls == []

    def test_a_transport_failure_is_unknown(self, miris_api):
        miris_api.responses.append(requests.ConnectionError("unreachable"))
        _, body, _ = self._invoke_per_view(miris_api)
        assert body["state"] == "unknown"

    def test_a_secret_read_failure_is_unknown_without_a_request(self, miris_api):
        miris_api.secretsmanager.get_secret_value.side_effect = RuntimeError("denied")
        _, body, _ = self._invoke_per_view(miris_api)
        assert body["state"] == "unknown"
        assert miris_api.calls == []

    def test_not_streamed_and_invalid_uuid_call_no_miris_api(self, miris_api):
        _, body, _ = self._invoke_per_view(miris_api, metadata_table=_metadata_table())
        assert body["state"] == "notStreamed"
        bad = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row(uuid="not-a-uuid")})
        _, body, _ = self._invoke_per_view(miris_api, metadata_table=bad)
        assert body["state"] == "invalidUuid"
        assert miris_api.calls == []
        miris_api.secretsmanager.get_secret_value.assert_not_called()

    def test_static_mode_reads_the_integration_api_and_then_the_viewer_api(self, miris_api):
        """Both modes check the binding with the Integration Key; static mode then asks the viewer
        API, with the deployment viewer key, for the state."""
        miris_api.responses.extend([
            (200, {"state": "preview", "tags": [BINDING_TAG, DATABASE_TAG]}),
            (200, {"state": "streamable"}),
        ])
        table = _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:/scene.usd": _stream_row()})
        _, body, _, _ = _invoke(_event(), metadata_table=table, viewer_api=False, mode="static")
        assert body["state"] == "streamable"
        assert [c["url"] for c in miris_api.calls] == [
            f"{miris_api.base_url}/v1/asset/{STORED_UUID}",
            f"{miris_api.base_url}/viewer/v1/asset/{STORED_UUID}",
        ]
        assert miris_api.calls[0]["headers"]["Miris-API-Key"] == miris_api.integration_key
        assert miris_api.calls[1]["headers"]["Miris-Viewer-Key"] == "viewer-key-0000000000"
        assert "Miris-API-Key" not in miris_api.calls[1]["headers"]
