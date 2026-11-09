# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""POST /addon/miris/viewer-key   body: {"databaseId": ..., "assetId": ..., "filePath": ...}

A viewer key is returned only after both authorization tiers allow it, only for the Miris asset
recorded on the file itself and tagged for this VAMS asset. In perView mode the key is minted and
returned only once its key ID is recorded in the key table; in static mode the deployment viewer key
is returned and nothing is minted or recorded. The key material is returned to the caller and appears
in no log line, audit entry, table row or error.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from backend.backend.handlers.addon.miris import mintViewerKey, mirisIntegration

from .conftest import FakeLambdaContext, claims_after_normalizing, rest_event as _rest_event

DATABASE_ID = "db-1"
ASSET_ID = "asset-1"
USER_ID = "alice@example.com"
STORED_UUID = "8d65af7c-5412-404b-a494-fbd2cdb62442"
OTHER_UUID = "bc336bab-e3e4-4c50-bd00-9d6cbbd9f194"
KEY_ID = "3f2b8c1e-9a4d-4e7f-8b6a-1c2d3e4f5a6b"
VIEWER_KEY_MATERIAL = "minted-viewer-key-material-do-not-log"
MIRIS_ERROR_BODY = "miris-error-body-must-not-surface"
ROUTE_PATH = "/addon/miris/viewer-key"
STATIC_VIEWER_KEY = "static-deployment-viewer-key-do-not-log-9e1d"
SORT_KEY_FIELD = "databaseId:assetId:filePath"
FIXED_NOW = datetime(2026, 10, 15, 18, 49, 28, 734000, tzinfo=timezone.utc)
FIXED_EPOCH_SECONDS = 1792090168.5


# Stand-ins for request_to_claims, which also normalizes the REST event the handler is given.
_AS_USER = claims_after_normalizing({"tokens": [USER_ID], "roles": []})
_AS_NOBODY = claims_after_normalizing({"tokens": [], "roles": []})


def _event(file_path="/scene.usd", method="POST", path=ROUTE_PATH, body=None, raw_body=None,
           database_id=DATABASE_ID, asset_id=ASSET_ID):
    if raw_body is None:
        payload = {"databaseId": database_id, "assetId": asset_id, "filePath": file_path}
        payload = {k: v for k, v in payload.items() if v is not None}
        payload.update(body or {})
        raw_body = json.dumps(payload)
    return _rest_event(method, path, body=raw_body)


def _metadata_table(rows_by_file=None):
    rows_by_file = rows_by_file or {}
    table = MagicMock()

    def get_item(Key):
        value = rows_by_file.get(Key[SORT_KEY_FIELD], {}).get(Key["metadataKey"])
        if value is None:
            return {}
        return {"Item": {"metadataKey": Key["metadataKey"], SORT_KEY_FIELD: Key[SORT_KEY_FIELD], "metadataValue": value}}

    table.get_item.side_effect = get_item
    return table


def _streamed(uuid=STORED_UUID, file_path="/scene.usd"):
    return _metadata_table({f"{DATABASE_ID}:{ASSET_ID}:{file_path}": {"miris_asset_uuid": uuid}})


def _asset_table(found=True, root_key=None):
    table = MagicMock()
    item = {"databaseId": DATABASE_ID, "assetId": ASSET_ID}
    if root_key is not None:
        item["assetLocation"] = {"Key": root_key}
    table.get_item.return_value = {"Item": item} if found else {}
    return table


def _enforcer(api=True, entity=True):
    enforcer = MagicMock()
    enforcer.enforceAPI.return_value = api
    enforcer.enforce.return_value = entity
    return enforcer


ASSET_TAG = f"vams-asset-{ASSET_ID}"
DATABASE_TAG = f"vams-database-{DATABASE_ID}"


def _asset_read(tags=("vams", ASSET_TAG, DATABASE_TAG), state="streamable"):
    return (200, {"id": STORED_UUID, "state": state, "tags": list(tags)})


BOUND = _asset_read()


def _created(key_id=KEY_ID):
    return {
        "id": key_id,
        "key": VIEWER_KEY_MATERIAL,
        "expires_at": "2026-10-15T19:04:28Z",
        "asset_ids": [STORED_UUID],
        "tags": None,
    }


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _invoke(event, miris_api, metadata_table=None, asset_table=None, key_table=None,
            enforcer=None, audit=None, ttl_minutes="15", context=None, mode="perView",
            static_key=STATIC_VIEWER_KEY):
    """Invoke the handler, in perView mode unless `mode` says otherwise."""
    metadata_table = metadata_table if metadata_table is not None else _streamed()
    asset_table = asset_table if asset_table is not None else _asset_table()
    key_table = key_table if key_table is not None else MagicMock()
    enforcer = enforcer if enforcer is not None else _enforcer()
    audit = audit if audit is not None else MagicMock()
    with patch.object(mintViewerKey, "asset_file_metadata_table", metadata_table), \
         patch.object(mintViewerKey, "asset_table", asset_table), \
         patch.object(mintViewerKey, "miris_viewer_key_table", key_table), \
         patch.object(mintViewerKey, "miris_viewer_key_mode", mode), \
         patch.object(mintViewerKey, "miris_static_viewer_key", static_key), \
         patch.object(mintViewerKey, "CasbinEnforcer", return_value=enforcer), \
         patch.object(mintViewerKey, "request_to_claims", side_effect=_AS_USER), \
         patch.object(mintViewerKey, "log_auth_other", audit), \
         patch.object(mintViewerKey, "viewer_key_ttl_minutes_setting", ttl_minutes), \
         patch.object(mintViewerKey, "datetime", _FixedDatetime), \
         patch.object(mintViewerKey.time, "time", return_value=FIXED_EPOCH_SECONDS):
        response = mintViewerKey.lambda_handler(event, context or FakeLambdaContext())
    return response, json.loads(response["body"])


def _creates(miris_api):
    return [c for c in miris_api.calls if c["method"] == "POST"]


def _reads(miris_api):
    return [c for c in miris_api.calls if c["method"] == "GET"]


def _deletes(miris_api):
    return [c for c in miris_api.calls if c["method"] == "DELETE"]


@pytest.mark.unit
class TestSuccess:

    def test_returns_the_key_and_its_expiry(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, body = _invoke(_event(), miris_api)

        assert response["statusCode"] == 200
        assert body == {
            "filePath": "/scene.usd",
            "mirisAssetUuid": STORED_UUID,
            "viewerKey": VIEWER_KEY_MATERIAL,
            "expiresAt": "2026-10-15T19:04:28Z",
        }

    def test_the_response_is_not_cacheable(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, _ = _invoke(_event(), miris_api)
        directives = [d.strip() for d in response["headers"]["Cache-Control"].split(",")]
        assert "no-store" in directives

    def test_creates_exactly_one_key_scoped_to_the_files_asset(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        _invoke(_event(), miris_api)

        creates = _creates(miris_api)
        assert len(creates) == 1
        assert creates[0]["url"] == f"{miris_api.base_url}/v1/asset-viewer-key"
        assert creates[0]["json"] == {
            "name": f"vams:{DATABASE_ID}:{ASSET_ID}:1792090168500",
            "tags": [],
            "asset_ids": [STORED_UUID],
            "expires_at": "2026-10-15T19:04:28Z",
        }
        assert [c["method"] for c in miris_api.calls] == ["GET", "POST"]

    @pytest.mark.parametrize("ttl_minutes, expires_at", [("1", "2026-10-15T18:50:28Z"), ("60", "2026-10-15T19:49:28Z")])
    def test_the_expiry_follows_the_configured_ttl(self, miris_api, ttl_minutes, expires_at):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, body = _invoke(_event(), miris_api, ttl_minutes=ttl_minutes)
        assert response["statusCode"] == 200
        assert body["expiresAt"] == expires_at
        assert _creates(miris_api)[0]["json"]["expires_at"] == expires_at

    def test_records_one_row_without_the_key_material(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        key_table = MagicMock()
        _invoke(_event(), miris_api, key_table=key_table)

        key_table.put_item.assert_called_once()
        kwargs = key_table.put_item.call_args.kwargs
        expiry_epoch = int(datetime(2026, 10, 15, 19, 4, 28, tzinfo=timezone.utc).timestamp())
        assert kwargs["Item"] == {
            "mirisViewerKeyId": KEY_ID,
            "databaseId": DATABASE_ID,
            "assetId": ASSET_ID,
            "filePath": "/scene.usd",
            "userId": USER_ID,
            "mirisAssetUuid": STORED_UUID,
            "createdAt": "2026-10-15T18:49:28Z",
            "expiresAt": "2026-10-15T19:04:28Z",
            "ttl": expiry_epoch + 3600,
        }
        # The put never overwrites the row of an existing key ID.
        condition = kwargs["ConditionExpression"]
        assert "attribute_not_exists" in condition and "mirisViewerKeyId" in condition
        assert VIEWER_KEY_MATERIAL not in json.dumps(kwargs)

    def test_audits_the_issuance_without_the_key_material(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        audit = MagicMock()
        event = _event()
        _invoke(event, miris_api, audit=audit)

        audit.assert_called_once()
        audit_event, secondary_type, data = audit.call_args.args
        assert audit_event is event
        assert secondary_type == "mirisViewerKeyMint"
        assert data == {
            "userId": USER_ID,
            "databaseId": DATABASE_ID,
            "assetId": ASSET_ID,
            "filePath": "/scene.usd",
            "mirisAssetUuid": STORED_UUID,
            "mirisViewerKeyId": KEY_ID,
            "expiresAt": "2026-10-15T19:04:28Z",
        }
        assert VIEWER_KEY_MATERIAL not in json.dumps(audit.call_args.args[1:])

    def test_a_full_object_key_is_normalized_against_the_assets_root(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, body = _invoke(
            _event(f"/assets/{ASSET_ID}/folder/scene.usd"),
            miris_api,
            metadata_table=_streamed(file_path="/folder/scene.usd"),
            asset_table=_asset_table(root_key=f"assets/{ASSET_ID}/"),
        )
        assert response["statusCode"] == 200
        assert body["filePath"] == "/folder/scene.usd"

    def test_a_client_supplied_uuid_is_ignored(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, body = _invoke(_event(body={"mirisAssetUuid": OTHER_UUID}), miris_api)
        assert response["statusCode"] == 200
        assert body["mirisAssetUuid"] == STORED_UUID
        assert _creates(miris_api)[0]["json"]["asset_ids"] == [STORED_UUID]


@pytest.mark.unit
class TestAuthorization:

    def test_tier1_denial_is_403_before_any_read_or_miris_call(self, miris_api):
        asset_table = _asset_table()
        metadata_table = _streamed()
        key_table = MagicMock()
        response, _ = _invoke(
            _event(), miris_api, asset_table=asset_table, metadata_table=metadata_table,
            key_table=key_table, enforcer=_enforcer(api=False),
        )
        assert response["statusCode"] == 403
        asset_table.get_item.assert_not_called()
        metadata_table.get_item.assert_not_called()
        key_table.put_item.assert_not_called()
        assert miris_api.calls == []

    def test_tier2_denial_is_403_before_any_metadata_read_or_miris_call(self, miris_api):
        metadata_table = _streamed()
        key_table = MagicMock()
        response, _ = _invoke(
            _event(), miris_api, metadata_table=metadata_table, key_table=key_table,
            enforcer=_enforcer(entity=False),
        )
        assert response["statusCode"] == 403
        metadata_table.get_item.assert_not_called()
        key_table.put_item.assert_not_called()
        assert miris_api.calls == []

    def test_tier2_is_checked_as_a_read_of_the_asset(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        enforcer = _enforcer()
        _invoke(_event(), miris_api, enforcer=enforcer)
        asset_arg, action = enforcer.enforce.call_args.args
        assert action == "GET"
        assert asset_arg["object__type"] == "asset"
        assert asset_arg["assetId"] == ASSET_ID

    def test_no_caller_identity_is_403(self, miris_api):
        with patch.object(mintViewerKey, "request_to_claims", side_effect=_AS_NOBODY), \
             patch.object(mintViewerKey, "miris_viewer_key_mode", "perView"), \
             patch.object(mintViewerKey, "miris_viewer_key_table", MagicMock()):
            response = mintViewerKey.lambda_handler(_event(), MagicMock())
        assert response["statusCode"] == 403
        assert miris_api.calls == []

    def test_a_missing_asset_is_400_without_a_miris_call(self, miris_api):
        response, body = _invoke(_event(), miris_api, asset_table=_asset_table(found=False))
        assert response["statusCode"] == 400
        assert body["message"] == "Asset not found"
        assert miris_api.calls == []


@pytest.mark.unit
class TestTheFilesStream:

    def test_no_stream_on_the_file_is_404_not_streamed(self, miris_api):
        response, body = _invoke(_event(), miris_api, metadata_table=_metadata_table())
        assert response["statusCode"] == 404
        assert body["state"] == "notStreamed"
        assert miris_api.calls == []

    def test_an_empty_uuid_is_404_not_streamed(self, miris_api):
        response, body = _invoke(_event(), miris_api, metadata_table=_streamed(uuid="  "))
        assert response["statusCode"] == 404
        assert body["state"] == "notStreamed"
        assert miris_api.calls == []

    def test_a_stream_on_another_file_is_not_used(self, miris_api):
        response, body = _invoke(_event("/scene.usd"), miris_api, metadata_table=_streamed(file_path="/other.usd"))
        assert response["statusCode"] == 404
        assert body["state"] == "notStreamed"
        assert miris_api.calls == []

    def test_a_non_uuid_is_400_invalid_uuid(self, miris_api):
        response, body = _invoke(_event(), miris_api, metadata_table=_streamed(uuid="not-a-uuid"))
        assert response["statusCode"] == 400
        assert body["state"] == "invalidUuid"
        assert miris_api.calls == []


@pytest.mark.unit
class TestBinding:
    """The Miris asset must carry both `vams-asset-{assetId}` and `vams-database-{databaseId}`
    before a key is minted for it."""

    def test_the_asset_is_read_with_the_integration_key_before_the_create(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created())])
        response, _ = _invoke(_event(), miris_api)

        assert response["statusCode"] == 200
        assert [(c["method"], c["url"]) for c in miris_api.calls] == [
            ("GET", f"{miris_api.base_url}/v1/asset/{STORED_UUID}"),
            ("POST", f"{miris_api.base_url}/v1/asset-viewer-key"),
        ]
        assert miris_api.calls[0]["headers"]["Miris-API-Key"] == miris_api.integration_key

    @pytest.mark.parametrize(
        "tags",
        [
            (),
            ("vams",),
            (ASSET_TAG,),
            (DATABASE_TAG,),
            ("vams", "vams-asset-asset-2", DATABASE_TAG),
            (f"{ASSET_TAG}-x", DATABASE_TAG),
        ],
        ids=["untagged", "vams-only", "asset-tag-only", "database-tag-only", "another-asset", "prefix-only"],
    )
    def test_an_unbound_asset_is_403_not_bound_and_nothing_is_minted(self, miris_api, tags):
        miris_api.responses.append(_asset_read(tags=tags))
        key_table = MagicMock()
        audit = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table, audit=audit)

        assert response["statusCode"] == 403
        assert body["state"] == "notBound"
        assert ASSET_TAG in body["message"] and DATABASE_TAG in body["message"]
        assert "viewerKey" not in body
        assert _creates(miris_api) == []
        key_table.put_item.assert_not_called()
        audit.assert_not_called()

    def test_the_same_asset_id_in_another_database_is_403_not_bound(self, miris_api):
        # The Miris asset belongs to asset-1 of db-2; this request is for asset-1 of db-1.
        miris_api.responses.append(_asset_read(tags=("vams", ASSET_TAG, "vams-database-db-2")))
        key_table = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table)

        assert response["statusCode"] == 403
        assert body["state"] == "notBound"
        assert _creates(miris_api) == []
        key_table.put_item.assert_not_called()

    def test_a_missing_miris_asset_is_403_not_bound_and_nothing_is_minted(self, miris_api):
        miris_api.responses.append(404)
        key_table = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table)
        assert response["statusCode"] == 403
        assert body["state"] == "notBound"
        assert _creates(miris_api) == []
        key_table.put_item.assert_not_called()

    @pytest.mark.parametrize(
        "outcome",
        [(500, MIRIS_ERROR_BODY), (200, {"tags": [ASSET_TAG, DATABASE_TAG]}), (200, MIRIS_ERROR_BODY)],
        ids=["500", "no-state", "non-json"],
    )
    def test_an_unreadable_asset_is_502_and_nothing_is_minted(self, miris_api, outcome):
        miris_api.responses.append(outcome)
        response, body = _invoke(_event(), miris_api)
        assert response["statusCode"] == 502
        assert MIRIS_ERROR_BODY not in response["body"]
        assert _creates(miris_api) == []


@pytest.fixture
def clock(monkeypatch, miris_api):
    """A controllable `time.monotonic`. `clock.per_request[method]` seconds pass during each
    Miris request of that method."""
    state = SimpleNamespace(now=1000.0, per_request={})
    monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: state.now)
    inner = mirisIntegration._get_session()

    def request(method, url, **kwargs):
        state.now += state.per_request.get(method, 0)
        return inner.request(method, url, **kwargs)

    monkeypatch.setattr(mirisIntegration, "_get_session", lambda: SimpleNamespace(request=request))
    return state


@pytest.mark.unit
class TestRequestDeadline:

    def test_too_little_time_left_is_504_without_a_miris_call(self, miris_api):
        key_table = MagicMock()
        response, body = _invoke(
            _event(), miris_api, key_table=key_table, context=FakeLambdaContext(remaining_ms=2000)
        )
        assert response["statusCode"] == 504
        assert "viewerKey" not in body
        assert miris_api.calls == []
        key_table.put_item.assert_not_called()

    def test_a_deadline_reached_after_the_read_is_504_without_a_create(self, miris_api, clock):
        clock.per_request["GET"] = 100
        miris_api.responses.append(BOUND)
        response, _ = _invoke(_event(), miris_api)
        assert response["statusCode"] == 504
        assert _creates(miris_api) == []

    def test_each_call_fits_in_the_time_left(self, miris_api, clock):
        clock.per_request["GET"] = 3
        miris_api.responses.extend([BOUND, (200, _created())])
        response, _ = _invoke(_event(), miris_api, context=FakeLambdaContext(remaining_ms=14000))

        assert response["statusCode"] == 200
        # The deadline is 11 s out: the read may use all 11 s, the create the 8 s left after it.
        read, create = miris_api.calls
        assert sum(read["timeout"]) == pytest.approx(11)
        assert sum(create["timeout"]) == pytest.approx(8)

    def test_a_key_without_material_is_deleted_even_after_the_deadline(self, miris_api, clock):
        # The create answers after the request deadline, with a key ID but no key material.
        clock.per_request["POST"] = 100
        miris_api.responses.extend([BOUND, (200, {"id": KEY_ID}), 204])
        key_table = MagicMock()
        response, _ = _invoke(_event(), miris_api, key_table=key_table)

        assert response["statusCode"] == 502
        deletes = _deletes(miris_api)
        assert [c["url"] for c in deletes] == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"]
        assert sum(deletes[0]["timeout"]) <= mirisIntegration._CLEANUP_GRACE_SECONDS
        key_table.put_item.assert_not_called()

    def test_an_unrecorded_key_is_deleted_even_after_the_deadline(self, miris_api, clock):
        miris_api.responses.extend([BOUND, (200, _created()), 204])
        key_table = MagicMock()

        def put_after_the_deadline(**kwargs):
            clock.now += 100
            raise RuntimeError("boom")

        key_table.put_item.side_effect = put_after_the_deadline
        response, _ = _invoke(_event(), miris_api, key_table=key_table)

        assert response["statusCode"] == 500
        deletes = _deletes(miris_api)
        assert [c["url"] for c in deletes] == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"]
        assert sum(deletes[0]["timeout"]) <= mirisIntegration._CLEANUP_GRACE_SECONDS


@pytest.mark.unit
class TestRobustness:

    def test_a_redirect_on_the_asset_read_fails_closed(self, miris_api):
        miris_api.responses.append((302, MIRIS_ERROR_BODY))
        key_table = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table)

        assert response["statusCode"] == 502
        assert MIRIS_ERROR_BODY not in response["body"]
        assert len(miris_api.calls) == 1
        assert miris_api.calls[0]["allow_redirects"] is False
        assert _creates(miris_api) == []
        key_table.put_item.assert_not_called()

    def test_a_request_received_long_ago_is_504_without_a_miris_call(self, miris_api):
        event = _event()
        # API Gateway received the request 27 s before the handler reads the clock.
        event["requestContext"]["requestTimeEpoch"] = int((FIXED_EPOCH_SECONDS - 27) * 1000)
        response, _ = _invoke(event, miris_api)
        assert response["statusCode"] == 504
        assert miris_api.calls == []

    def test_the_key_table_write_is_short(self):
        config = mintViewerKey.key_table_dynamodb.meta.client.meta.config
        assert config.connect_timeout == 1
        assert config.read_timeout == 2
        assert config.retries["total_max_attempts"] == 2


@pytest.mark.unit
class TestKeyName:

    def test_a_long_asset_id_is_hashed_into_a_capped_name(self, miris_api):
        long_asset_id = "a" * 255
        event = _event(asset_id=long_asset_id)
        table = _metadata_table({f"{DATABASE_ID}:{long_asset_id}:/scene.usd": {"miris_asset_uuid": STORED_UUID}})
        miris_api.responses.extend([
            (200, {"state": "streamable", "tags": [f"vams-asset-{long_asset_id}", DATABASE_TAG]}),
            (200, _created()),
        ])
        response, _ = _invoke(event, miris_api, metadata_table=table)

        assert response["statusCode"] == 200
        name = _creates(miris_api)[0]["json"]["name"]
        assert len(name) <= 128
        assert long_asset_id not in name
        assert name.startswith("vams:") and name.endswith(":1792090168500")

    def test_names_of_different_long_identities_differ(self):
        first = mirisIntegration.viewer_key_name(DATABASE_ID, "a" * 256, 1)
        second = mirisIntegration.viewer_key_name(DATABASE_ID, "a" * 255 + "b", 1)
        assert first != second
        assert max(len(first), len(second)) <= mirisIntegration.MAX_VIEWER_KEY_NAME_LENGTH


@pytest.mark.unit
class TestRequestValidation:

    @pytest.mark.parametrize(
        "event",
        [
            _event(None),
            _event(""),
            _event("/"),
            _event("/../secret.usd"),
            _event("/folder/"),
            _event(raw_body="{not json"),
            _event(raw_body="[]"),
            _event(raw_body=""),
            _event(database_id=None),
            _event(asset_id=None),
            _event(database_id="db 1"),
            _event(database_id="d" * 300),
            _event(asset_id="../asset-2"),
            _event(asset_id="a" * 300),
        ],
        ids=["missing", "empty", "root", "traversal", "folder", "bad-json", "array", "empty-body",
             "no-database", "no-asset", "database-space", "database-too-long", "asset-traversal",
             "asset-too-long"],
    )
    def test_an_invalid_request_is_400_without_any_read(self, miris_api, event):
        asset_table = _asset_table()
        response, _ = _invoke(event, miris_api, asset_table=asset_table)
        assert response["statusCode"] == 400
        asset_table.get_item.assert_not_called()
        assert miris_api.calls == []

    def test_ids_in_path_parameters_are_not_read(self, miris_api):
        """The asset is named by the body only; path parameters carry nothing."""
        asset_table = _asset_table()
        event = _event(database_id=None, asset_id=None)
        event["pathParameters"] = {"databaseId": DATABASE_ID, "assetId": ASSET_ID}
        response, _ = _invoke(event, miris_api, asset_table=asset_table)
        assert response["statusCode"] == 400
        asset_table.get_item.assert_not_called()
        assert miris_api.calls == []

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_other_methods_are_rejected(self, miris_api, method):
        response, _ = _invoke(_event(method=method), miris_api)
        assert response["statusCode"] == 400
        assert miris_api.calls == []

    def test_the_status_route_is_not_dispatched(self, miris_api):
        status_path = "/addon/miris/asset-status"
        response, _ = _invoke(_event(path=status_path), miris_api)
        assert response["statusCode"] == 400
        assert miris_api.calls == []

    @pytest.mark.parametrize("ttl_minutes", ["", "0", "61", "15.5", "abc"])
    def test_a_bad_ttl_setting_is_500_without_a_miris_call(self, miris_api, ttl_minutes):
        response, _ = _invoke(_event(), miris_api, ttl_minutes=ttl_minutes)
        assert response["statusCode"] == 500
        assert miris_api.calls == []


@pytest.mark.unit
class TestFailures:

    @pytest.mark.parametrize(
        "outcomes",
        [
            [(500, MIRIS_ERROR_BODY)],
            [(400, MIRIS_ERROR_BODY)],
            [(200, {"id": KEY_ID}), 204],
            [(200, MIRIS_ERROR_BODY)],
        ],
        ids=["500", "400", "no-key", "non-json"],
    )
    def test_a_miris_failure_is_502_without_the_miris_body(self, miris_api, outcomes):
        miris_api.responses.extend([BOUND] + outcomes)
        key_table = MagicMock()
        audit = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table, audit=audit)

        assert response["statusCode"] == 502
        assert MIRIS_ERROR_BODY not in response["body"]
        assert "viewerKey" not in body
        key_table.put_item.assert_not_called()
        audit.assert_not_called()

    def test_a_transport_failure_is_502(self, miris_api):
        import requests

        miris_api.responses.extend([BOUND, requests.ConnectionError(MIRIS_ERROR_BODY)])
        response, _ = _invoke(_event(), miris_api)
        assert response["statusCode"] == 502
        assert MIRIS_ERROR_BODY not in response["body"]

    @pytest.mark.parametrize(
        "error",
        [
            ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"),
            ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "PutItem"),
            RuntimeError("boom"),
        ],
        ids=["duplicate-id", "throttled", "other"],
    )
    def test_a_failed_table_put_deletes_the_minted_key_and_is_500(self, miris_api, error):
        miris_api.responses.extend([BOUND, (200, _created()), 204])
        key_table = MagicMock()
        key_table.put_item.side_effect = error
        audit = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table, audit=audit)

        assert response["statusCode"] == 500
        assert VIEWER_KEY_MATERIAL not in response["body"]
        deletes = _deletes(miris_api)
        assert [c["url"] for c in deletes] == [f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"]
        audit.assert_not_called()

    def test_a_key_returned_without_material_is_deleted_and_is_502(self, miris_api):
        miris_api.responses.extend([BOUND, (200, {"id": KEY_ID}), 204])
        key_table = MagicMock()
        response, _ = _invoke(_event(), miris_api, key_table=key_table)

        assert response["statusCode"] == 502
        assert [c["url"] for c in _deletes(miris_api)] == [
            f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"]
        key_table.put_item.assert_not_called()

    def test_an_unexpected_failure_after_the_mint_logs_only_its_type(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([BOUND, (200, _created())])
        audit = MagicMock(side_effect=RuntimeError(f"boom {VIEWER_KEY_MATERIAL}"))
        response, _ = _invoke(_event(), miris_api, audit=audit)

        assert response["statusCode"] == 500
        assert VIEWER_KEY_MATERIAL not in response["body"]
        audit.assert_called_once()
        # Control: the catch-all logged the failure, by its type.
        assert "Internal error (RuntimeError)" in log_recorder.text()
        for logged in (log_recorder.text(), caplog.text):
            assert VIEWER_KEY_MATERIAL not in logged
            assert "boom" not in logged

    def test_the_error_response_is_built_outside_the_exception(self, miris_api):
        """The 500 response writes an audit entry, and a failed write logs a traceback. Built while
        the exception is still being handled, that traceback would chain the exception's message."""
        miris_api.responses.extend([BOUND, (200, _created())])
        audit = MagicMock(side_effect=RuntimeError(f"boom {VIEWER_KEY_MATERIAL}"))
        handled_at_call = []
        real_internal_error = mintViewerKey.internal_error

        def recording_internal_error(*args, **kwargs):
            handled_at_call.append(sys.exc_info()[1])
            return real_internal_error(*args, **kwargs)

        with patch.object(mintViewerKey, "internal_error", side_effect=recording_internal_error):
            response, _ = _invoke(_event(), miris_api, audit=audit)

        assert response["statusCode"] == 500
        assert handled_at_call == [None]

    def test_the_catch_all_does_not_log_a_traceback(self, miris_api, log_recorder):
        miris_api.responses.extend([BOUND, (200, _created())])
        audit = MagicMock(side_effect=RuntimeError("boom"))
        _invoke(_event(), miris_api, audit=audit)
        assert not any(line.startswith("exception:") for line in log_recorder.lines)

    def test_a_failed_cleanup_delete_still_returns_500(self, miris_api):
        miris_api.responses.extend([BOUND, (200, _created()), (503, MIRIS_ERROR_BODY)])
        key_table = MagicMock()
        key_table.put_item.side_effect = RuntimeError("boom")
        response, _ = _invoke(_event(), miris_api, key_table=key_table)
        assert response["statusCode"] == 500
        assert len(_deletes(miris_api)) == 1


@pytest.mark.unit
class TestNoKeyMaterialInLogs:

    def test_no_path_logs_the_key_material_or_a_secret(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        failing_table = MagicMock()
        failing_table.put_item.side_effect = RuntimeError(f"boom {VIEWER_KEY_MATERIAL}")
        miris_api.responses.extend([
            BOUND, (200, _created()),                 # success
            BOUND, (200, _created()), (503, MIRIS_ERROR_BODY),  # put fails, cleanup delete fails
            BOUND, (500, MIRIS_ERROR_BODY),            # create fails
        ])

        _invoke(_event(), miris_api)
        _invoke(_event(), miris_api, key_table=failing_table)
        _invoke(_event(), miris_api)

        # Control: each path was logged, so an empty log is not what passes.
        text = log_recorder.text()
        assert f"Created Miris viewer key {KEY_ID}" in text
        assert f"Could not record Miris viewer key {KEY_ID}" in text
        assert f"Could not delete unrecorded Miris viewer key {KEY_ID}" in text
        assert "HTTP 500" in text
        for logged in (text, caplog.text):
            assert VIEWER_KEY_MATERIAL not in logged
            assert miris_api.integration_key not in logged
            assert MIRIS_ERROR_BODY not in logged


@pytest.mark.unit
class TestStaticModeDeliversTheDeploymentKey:
    """In static mode the deployment viewer key is returned after Tier 1, Tier 2 and the binding
    check, and nothing is minted or recorded."""

    def test_a_bound_asset_returns_the_deployment_key(self, miris_api):
        miris_api.responses.append(BOUND)
        key_table = MagicMock()
        response, body = _invoke(_event(), miris_api, key_table=key_table, mode="static")

        assert response["statusCode"] == 200
        assert body == {
            "filePath": "/scene.usd",
            "mirisAssetUuid": STORED_UUID,
            "viewerKey": STATIC_VIEWER_KEY,
            "expiresAt": None,
        }
        directives = [d.strip() for d in response["headers"]["Cache-Control"].split(",")]
        assert "no-store" in directives

    def test_the_binding_is_read_and_no_key_is_minted_or_recorded(self, miris_api):
        miris_api.responses.append(BOUND)
        key_table = MagicMock()
        _invoke(_event(), miris_api, key_table=key_table, mode="static")

        assert [(c["method"], c["url"]) for c in miris_api.calls] == [
            ("GET", f"{miris_api.base_url}/v1/asset/{STORED_UUID}"),
        ]
        assert miris_api.calls[0]["headers"]["Miris-API-Key"] == miris_api.integration_key
        assert STATIC_VIEWER_KEY not in json.dumps(miris_api.calls[0]["headers"])
        key_table.put_item.assert_not_called()

    def test_the_static_key_needs_no_ttl_setting(self, miris_api):
        miris_api.responses.append(BOUND)
        response, body = _invoke(_event(), miris_api, mode="static", ttl_minutes="")
        assert response["statusCode"] == 200
        assert body["viewerKey"] == STATIC_VIEWER_KEY

    def test_the_issue_is_audited_without_the_key(self, miris_api):
        miris_api.responses.append(BOUND)
        audit = MagicMock()
        event = _event()
        _invoke(event, miris_api, audit=audit, mode="static")

        audit.assert_called_once()
        audit_event, secondary_type, data = audit.call_args.args
        assert audit_event is event
        assert secondary_type == "mirisStaticViewerKeyIssue"
        assert data == {
            "userId": USER_ID,
            "databaseId": DATABASE_ID,
            "assetId": ASSET_ID,
            "filePath": "/scene.usd",
            "mirisAssetUuid": STORED_UUID,
        }
        assert STATIC_VIEWER_KEY not in json.dumps(audit.call_args.args[1:])

    @pytest.mark.parametrize(
        "tags",
        [
            (),
            ("vams",),
            (ASSET_TAG,),
            (DATABASE_TAG,),
            ("vams", "vams-asset-asset-2", DATABASE_TAG),
            ("vams", ASSET_TAG, "vams-database-db-2"),
            (f"{ASSET_TAG}-x", DATABASE_TAG),
        ],
        ids=["untagged", "vams-only", "asset-tag-only", "database-tag-only", "another-asset",
             "same-asset-id-other-database", "prefix-only"],
    )
    def test_an_unbound_asset_is_403_not_bound_without_the_key(self, miris_api, tags):
        miris_api.responses.append(_asset_read(tags=tags))
        audit = MagicMock()
        response, body = _invoke(_event(), miris_api, audit=audit, mode="static")

        assert response["statusCode"] == 403
        assert body["state"] == "notBound"
        assert "viewerKey" not in body
        assert STATIC_VIEWER_KEY not in response["body"]
        audit.assert_not_called()

    def test_a_missing_miris_asset_is_403_not_bound_without_the_key(self, miris_api):
        miris_api.responses.append(404)
        response, body = _invoke(_event(), miris_api, mode="static")
        assert response["statusCode"] == 403
        assert body["state"] == "notBound"
        assert STATIC_VIEWER_KEY not in response["body"]

    @pytest.mark.parametrize(
        "outcome",
        [(500, MIRIS_ERROR_BODY), (302, MIRIS_ERROR_BODY), (200, {"tags": [ASSET_TAG, DATABASE_TAG]})],
        ids=["500", "redirect", "no-state"],
    )
    def test_an_unreadable_binding_is_502_without_the_key(self, miris_api, outcome):
        miris_api.responses.append(outcome)
        response, _ = _invoke(_event(), miris_api, mode="static")
        assert response["statusCode"] == 502
        assert STATIC_VIEWER_KEY not in response["body"]
        assert MIRIS_ERROR_BODY not in response["body"]

    def test_too_little_time_left_is_504_without_the_key(self, miris_api):
        response, _ = _invoke(
            _event(), miris_api, mode="static", context=FakeLambdaContext(remaining_ms=2000)
        )
        assert response["statusCode"] == 504
        assert STATIC_VIEWER_KEY not in response["body"]
        assert miris_api.calls == []

    def test_tier1_denial_returns_no_key(self, miris_api):
        asset_table = _asset_table()
        response, _ = _invoke(
            _event(), miris_api, asset_table=asset_table, enforcer=_enforcer(api=False), mode="static"
        )
        assert response["statusCode"] == 403
        assert STATIC_VIEWER_KEY not in response["body"]
        asset_table.get_item.assert_not_called()
        assert miris_api.calls == []

    def test_tier2_denial_returns_no_key(self, miris_api):
        metadata_table = _streamed()
        audit = MagicMock()
        response, _ = _invoke(
            _event(), miris_api, metadata_table=metadata_table, enforcer=_enforcer(entity=False),
            audit=audit, mode="static",
        )
        assert response["statusCode"] == 403
        assert STATIC_VIEWER_KEY not in response["body"]
        metadata_table.get_item.assert_not_called()
        assert miris_api.calls == []
        audit.assert_not_called()

    def test_tier2_is_checked_as_a_read_of_the_asset(self, miris_api):
        miris_api.responses.append(BOUND)
        enforcer = _enforcer()
        _invoke(_event(), miris_api, enforcer=enforcer, mode="static")
        asset_arg, action = enforcer.enforce.call_args.args
        assert action == "GET"
        assert asset_arg["object__type"] == "asset"
        assert asset_arg["assetId"] == ASSET_ID
        assert asset_arg["databaseId"] == DATABASE_ID

    @pytest.mark.parametrize(
        "metadata_table, status",
        [(_metadata_table(), 404), (_streamed(uuid="not-a-uuid"), 400)],
        ids=["not-streamed", "invalid-uuid"],
    )
    def test_a_file_without_a_usable_stream_returns_no_key(self, miris_api, metadata_table, status):
        response, _ = _invoke(_event(), miris_api, metadata_table=metadata_table, mode="static")
        assert response["statusCode"] == status
        assert STATIC_VIEWER_KEY not in response["body"]
        assert miris_api.calls == []

    def test_the_key_never_reaches_a_log_line(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([
            BOUND,                                   # key returned
            _asset_read(tags=("vams",)),             # not bound
            404,                                     # Miris has no such asset
            (500, MIRIS_ERROR_BODY),                 # binding unreadable
            BOUND,                                   # bound, then the audit write fails
        ])
        failing_audit = MagicMock(side_effect=RuntimeError(f"boom {STATIC_VIEWER_KEY}"))
        statuses = [
            _invoke(_event(), miris_api, mode="static")[0]["statusCode"],
            _invoke(_event(), miris_api, mode="static")[0]["statusCode"],
            _invoke(_event(), miris_api, mode="static")[0]["statusCode"],
            _invoke(_event(), miris_api, mode="static")[0]["statusCode"],
            _invoke(_event(), miris_api, mode="static", enforcer=_enforcer(entity=False))[0]["statusCode"],
            _invoke(_event(), miris_api, mode="static", audit=failing_audit)[0]["statusCode"],
        ]
        assert statuses == [200, 403, 403, 502, 403, 500]

        # Control: the paths were logged, so an empty log is not what passes.
        text = log_recorder.text()
        assert "not tagged" in text
        assert "HTTP 500" in text
        assert "Internal error (RuntimeError)" in text
        for logged in (text, caplog.text):
            assert STATIC_VIEWER_KEY not in logged
            assert miris_api.integration_key not in logged
            assert "boom" not in logged


@pytest.mark.unit
class TestNotConfigured:
    """The Integration Key is required in both modes, so the route fails closed with 503 when any
    setting the mode needs is absent, and returns no key."""

    @pytest.mark.parametrize("mode", ["static", "perView"])
    @pytest.mark.parametrize("missing", ["MIRIS_API_KEY_SECRET_ARN", "MIRIS_API_BASE_URL"])
    def test_a_missing_integration_setting_is_503(self, miris_api, monkeypatch, mode, missing):
        monkeypatch.setattr(mirisIntegration, missing, "")
        asset_table = _asset_table()
        key_table = MagicMock()
        response, body = _invoke(
            _event(), miris_api, asset_table=asset_table, key_table=key_table, mode=mode
        )
        assert response["statusCode"] == 503
        assert "viewerKey" not in body
        assert STATIC_VIEWER_KEY not in response["body"]
        asset_table.get_item.assert_not_called()
        key_table.put_item.assert_not_called()
        assert miris_api.calls == []
        miris_api.secretsmanager.get_secret_value.assert_not_called()

    def test_static_mode_without_the_deployment_key_is_503(self, miris_api):
        asset_table = _asset_table()
        response, body = _invoke(_event(), miris_api, asset_table=asset_table, mode="static", static_key="")
        assert response["statusCode"] == 503
        assert "viewerKey" not in body
        asset_table.get_item.assert_not_called()
        assert miris_api.calls == []

    def test_per_view_mode_without_the_key_table_is_503(self, miris_api):
        with patch.object(mintViewerKey, "miris_viewer_key_table", None), \
             patch.object(mintViewerKey, "miris_viewer_key_mode", "perView"), \
             patch.object(mintViewerKey, "CasbinEnforcer", return_value=_enforcer()), \
             patch.object(mintViewerKey, "request_to_claims", side_effect=_AS_USER):
            response = mintViewerKey.lambda_handler(_event(), FakeLambdaContext())
        assert response["statusCode"] == 503
        assert miris_api.calls == []

    @pytest.mark.parametrize("mode", ["", "Static", "perview", "per-view"])
    def test_an_unrecognized_mode_is_503(self, miris_api, mode):
        asset_table = _asset_table()
        response, body = _invoke(_event(), miris_api, asset_table=asset_table, mode=mode)
        assert response["statusCode"] == 503
        assert "viewerKey" not in body
        assert STATIC_VIEWER_KEY not in response["body"]
        asset_table.get_item.assert_not_called()
        assert miris_api.calls == []

    def test_tier1_is_checked_before_the_configuration(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration, "MIRIS_API_KEY_SECRET_ARN", "")
        response, _ = _invoke(_event(), miris_api, enforcer=_enforcer(api=False))
        assert response["statusCode"] == 403


def _load_handler_copy(monkeypatch, mode):
    """A private copy of the handler module, imported with `MIRIS_VIEWER_KEY_MODE` set to `mode`."""
    import importlib.util
    import types

    # The root conftest swaps these packages for bare stand-ins before each test; the copy only needs
    # the names it imports.
    for module_name, attribute in (("handlers.auth", "request_to_claims"),
                                   ("handlers.authz", "CasbinEnforcer"),
                                   ("customLogging.auditLogging", "log_auth_other")):
        stand_in = types.ModuleType(module_name)
        setattr(stand_in, attribute, MagicMock())
        monkeypatch.setitem(sys.modules, module_name, stand_in)
    monkeypatch.setenv("MIRIS_VIEWER_KEY_MODE", mode)
    name = f"{mintViewerKey.__package__}._mint_viewer_key_load_probe"
    spec = importlib.util.spec_from_file_location(name, mintViewerKey.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestModuleLoad:
    """The key table is resolved from the add-on resource-name key in perView mode only."""

    def test_per_view_mode_resolves_the_add_on_key_table(self, monkeypatch):
        monkeypatch.setenv("MIRIS_VIEWER_KEY_STORAGE_TABLE_NAME", "probe-viewer-key-table")
        module = _load_handler_copy(monkeypatch, "perView")
        assert module.miris_viewer_key_table_name == "probe-viewer-key-table"
        assert module.miris_viewer_key_table.name == "probe-viewer-key-table"

    def test_static_mode_needs_no_key_table(self, monkeypatch):
        monkeypatch.setenv("MIRIS_VIEWER_KEY_STORAGE_TABLE_NAME", "probe-viewer-key-table")
        module = _load_handler_copy(monkeypatch, "static")
        assert module.miris_viewer_key_table_name is None
        assert module.miris_viewer_key_table is None

    def test_the_key_table_is_an_add_on_resource_name(self):
        from common.addonResourceNames import AddonResourceKeys

        key = AddonResourceKeys.MIRIS_VIEWER_KEY_STORAGE_TABLE
        assert key.param_key == "addon/miris/viewerKeyStorage"
        assert key.env_var_names == ("MIRIS_VIEWER_KEY_STORAGE_TABLE_NAME",)
