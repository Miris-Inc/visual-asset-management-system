# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The shared Miris Integration API client: request shape, status handling, Integration Key
caching and rotation, and that no key or response body reaches a log line or exception message."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from backend.backend.handlers.addon.miris import mirisIntegration
from backend.backend.handlers.addon.miris.mirisIntegration import MirisIntegrationError

from .conftest import FakeLambdaContext

KEY_ID = "3f2b8c1e-9a4d-4e7f-8b6a-1c2d3e4f5a6b"
RESPONSE_BODY = "miris-response-body-must-not-surface"


class TestDeleteViewerKeyRequest:
    def test_sends_one_delete_with_the_integration_key_header_and_a_timeout(self, miris_api):
        miris_api.responses.append(204)

        mirisIntegration.delete_viewer_key(KEY_ID)

        assert len(miris_api.calls) == 1
        call = miris_api.calls[0]
        assert call["method"] == "DELETE"
        assert call["url"] == f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"
        assert call["headers"]["Miris-API-Key"] == miris_api.integration_key
        assert call["timeout"] == mirisIntegration._REQUEST_TIMEOUT_SECONDS
        assert call["timeout"] is not None
        miris_api.secretsmanager.get_secret_value.assert_called_once_with(SecretId=miris_api.secret_arn)

    @pytest.mark.parametrize("status", [200, 204, 404])
    def test_success_statuses_return_without_raising(self, miris_api, status):
        miris_api.responses.append(status)
        assert mirisIntegration.delete_viewer_key(KEY_ID) is None

    @pytest.mark.parametrize("status", [400, 409, 429, 500, 502, 503])
    def test_other_statuses_raise_with_the_status_code_only(self, miris_api, status):
        miris_api.responses.append((status, RESPONSE_BODY))

        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.delete_viewer_key(KEY_ID)

        assert raised.value.status_code == status
        assert str(status) in str(raised.value)
        assert RESPONSE_BODY not in str(raised.value)
        assert miris_api.integration_key not in str(raised.value)

    def test_transport_failure_raises_without_the_key_or_url_detail(self, miris_api):
        miris_api.responses.append(
            requests.ConnectionError(f"boom {miris_api.integration_key} {RESPONSE_BODY}")
        )

        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.delete_viewer_key(KEY_ID)

        assert raised.value.status_code is None
        assert "ConnectionError" in str(raised.value)
        assert miris_api.integration_key not in str(raised.value)
        assert RESPONSE_BODY not in str(raised.value)
        assert raised.value.__cause__ is None

    def test_timeout_raises(self, miris_api):
        miris_api.responses.append(requests.Timeout())
        with pytest.raises(MirisIntegrationError, match="Timeout"):
            mirisIntegration.delete_viewer_key(KEY_ID)


class TestKeyIdValidation:
    @pytest.mark.parametrize(
        "key_id",
        [
            "",
            None,
            "not-a-uuid",
            "../asset-viewer-key",
            f"{KEY_ID}/../other",
            f"{KEY_ID}?x=1",
            KEY_ID.replace("-", ""),
            "{" + KEY_ID + "}",
            f"{KEY_ID}\n",
        ],
    )
    def test_a_non_uuid_raises_before_any_secret_read_or_request(self, miris_api, key_id):
        with pytest.raises(MirisIntegrationError, match="not a UUID"):
            mirisIntegration.delete_viewer_key(key_id)
        assert miris_api.calls == []
        miris_api.secretsmanager.get_secret_value.assert_not_called()

    def test_an_uppercase_uuid_is_accepted(self, miris_api):
        miris_api.responses.append(204)
        mirisIntegration.delete_viewer_key(KEY_ID.upper())
        assert len(miris_api.calls) == 1


class TestIntegrationKey:
    def test_the_key_is_cached_across_calls(self, miris_api):
        miris_api.responses.extend([204, 204])
        mirisIntegration.delete_viewer_key(KEY_ID)
        mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.secretsmanager.get_secret_value.call_count == 1

    def test_the_key_is_read_again_after_the_cache_window(self, miris_api, monkeypatch):
        miris_api.responses.extend([204, 204])
        mirisIntegration.delete_viewer_key(KEY_ID)
        monkeypatch.setattr(mirisIntegration, "_SECRET_CACHE_TTL_SECONDS", -1)
        mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.secretsmanager.get_secret_value.call_count == 2

    @pytest.mark.parametrize("status", [401, 403])
    def test_an_auth_failure_rereads_the_key_and_retries_once(self, miris_api, status):
        rotated = "rotated-integration-key-do-not-log"
        miris_api.secretsmanager.get_secret_value.side_effect = [
            {"SecretString": miris_api.integration_key},
            {"SecretString": rotated},
        ]
        miris_api.responses.extend([status, 204])

        mirisIntegration.delete_viewer_key(KEY_ID)

        assert [c["headers"]["Miris-API-Key"] for c in miris_api.calls] == [
            miris_api.integration_key,
            rotated,
        ]

    def test_a_second_auth_failure_raises(self, miris_api):
        miris_api.responses.extend([401, 401])
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.delete_viewer_key(KEY_ID)
        assert raised.value.status_code == 401
        assert len(miris_api.calls) == 2

    def test_a_secrets_manager_failure_raises_without_calling_miris(self, miris_api):
        miris_api.secretsmanager.get_secret_value.side_effect = RuntimeError(
            f"secret detail {miris_api.integration_key}"
        )
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.delete_viewer_key(KEY_ID)
        assert "RuntimeError" in str(raised.value)
        assert miris_api.integration_key not in str(raised.value)
        assert miris_api.calls == []

    def test_an_empty_secret_raises(self, miris_api):
        miris_api.secretsmanager.get_secret_value.return_value = {"SecretString": "  "}
        with pytest.raises(MirisIntegrationError, match="empty"):
            mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.calls == []

    def test_a_secret_with_surrounding_whitespace_is_trimmed(self, miris_api):
        miris_api.secretsmanager.get_secret_value.return_value = {
            "SecretString": f"  {miris_api.integration_key}\n"
        }
        miris_api.responses.append(204)
        mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.calls[0]["headers"]["Miris-API-Key"] == miris_api.integration_key


class TestConfiguration:
    def test_a_missing_base_url_raises_without_a_request(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration, "MIRIS_API_BASE_URL", "")
        with pytest.raises(MirisIntegrationError, match="MIRIS_API_BASE_URL"):
            mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.calls == []

    def test_a_missing_secret_arn_raises_without_a_request(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration, "MIRIS_API_KEY_SECRET_ARN", "")
        with pytest.raises(MirisIntegrationError, match="MIRIS_API_KEY_SECRET_ARN"):
            mirisIntegration.delete_viewer_key(KEY_ID)
        assert miris_api.calls == []
        miris_api.secretsmanager.get_secret_value.assert_not_called()

    def test_integration_configured_needs_both_settings(self, miris_api, monkeypatch):
        assert mirisIntegration.integration_configured() is True
        for missing in ("MIRIS_API_KEY_SECRET_ARN", "MIRIS_API_BASE_URL"):
            with monkeypatch.context() as patched:
                patched.setattr(mirisIntegration, missing, "")
                assert mirisIntegration.integration_configured() is False


class TestNoSecretInLogs:
    def test_no_path_logs_the_integration_key_or_a_response_body(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([404, (401, RESPONSE_BODY), 204, (500, RESPONSE_BODY)])

        mirisIntegration.delete_viewer_key(KEY_ID)
        mirisIntegration.delete_viewer_key(KEY_ID)
        with pytest.raises(MirisIntegrationError):
            mirisIntegration.delete_viewer_key(KEY_ID)

        # Control: the recorder captured the 404 and 401 lines, so an empty log is not what passes.
        assert any(KEY_ID in line for line in log_recorder.lines)
        assert any("401" in line for line in log_recorder.lines)
        for text in (log_recorder.text(), caplog.text):
            assert miris_api.integration_key not in text
            assert RESPONSE_BODY not in text


ASSET_UUID = "8d65af7c-5412-404b-a494-fbd2cdb62442"
VIEWER_KEY_MATERIAL = "minted-viewer-key-material-do-not-log"
EXPIRES_AT = datetime(2026, 10, 15, 19, 4, 28, tzinfo=timezone.utc)


def _created(key_id=KEY_ID, key=VIEWER_KEY_MATERIAL):
    return {
        "id": key_id,
        "key": key,
        "name": "vams:db:asset:1",
        "expires_at": "2026-10-15T19:04:28Z",
        "asset_ids": [ASSET_UUID],
        "tags": None,
    }


class TestCreateViewerKey:
    def test_sends_one_post_scoped_to_exactly_the_asset(self, miris_api):
        miris_api.responses.append((200, _created()))

        mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "vams:db:asset:1")

        assert len(miris_api.calls) == 1
        call = miris_api.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"{miris_api.base_url}/v1/asset-viewer-key"
        assert call["headers"]["Miris-API-Key"] == miris_api.integration_key
        assert call["timeout"] == mirisIntegration._REQUEST_TIMEOUT_SECONDS
        assert call["json"] == {
            "name": "vams:db:asset:1",
            "tags": [],
            "asset_ids": [ASSET_UUID],
            "expires_at": "2026-10-15T19:04:28Z",
        }

    def test_returns_the_key_id_material_and_requested_expiry(self, miris_api):
        miris_api.responses.append((200, _created()))
        minted = mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert minted.key_id == KEY_ID
        assert minted.key == VIEWER_KEY_MATERIAL
        assert minted.expires_at == "2026-10-15T19:04:28Z"

    def test_the_key_material_is_not_in_the_repr(self, miris_api):
        miris_api.responses.append((200, _created()))
        minted = mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert VIEWER_KEY_MATERIAL not in repr(minted)
        assert KEY_ID in repr(minted)

    def test_an_expiry_in_another_zone_is_sent_as_utc(self, miris_api):
        miris_api.responses.append((200, _created()))
        local = EXPIRES_AT.astimezone(timezone(timedelta(hours=-7)))
        mirisIntegration.create_viewer_key(ASSET_UUID, local, "n")
        assert miris_api.calls[0]["json"]["expires_at"] == "2026-10-15T19:04:28Z"

    @pytest.mark.parametrize(
        "body",
        [
            {"id": KEY_ID},
            {"id": KEY_ID, "key": ""},
            {"id": KEY_ID, "key": "   "},
            {"id": KEY_ID, "key": 123},
            {"id": "not-a-uuid", "key": VIEWER_KEY_MATERIAL},
            {"key": VIEWER_KEY_MATERIAL},
            [VIEWER_KEY_MATERIAL],
            f"<html>{VIEWER_KEY_MATERIAL}</html>",
        ],
        ids=["no-key", "empty-key", "blank-key", "non-string-key", "bad-id", "no-id", "list", "non-json"],
    )
    def test_a_response_without_a_usable_id_and_key_raises_without_echoing_it(self, miris_api, body):
        # The 204 answers the delete a response with a UUID id triggers.
        miris_api.responses.extend([(200, body), 204])
        with pytest.raises(MirisIntegrationError, match="no usable key") as raised:
            mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert VIEWER_KEY_MATERIAL not in str(raised.value)

    @pytest.mark.parametrize(
        "body",
        [{"id": KEY_ID}, {"id": KEY_ID, "key": ""}, {"id": KEY_ID, "key": "   "}, {"id": KEY_ID, "key": 123}],
        ids=["no-key", "empty-key", "blank-key", "non-string-key"],
    )
    def test_a_key_created_without_usable_material_is_deleted_at_once(self, miris_api, body):
        miris_api.responses.extend([(201, body), 204])
        with pytest.raises(MirisIntegrationError, match="no usable key") as raised:
            mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert raised.value.status_code == 201
        assert [(c["method"], c["url"]) for c in miris_api.calls] == [
            ("POST", f"{miris_api.base_url}/v1/asset-viewer-key"),
            ("DELETE", f"{miris_api.base_url}/v1/asset-viewer-key/{KEY_ID}"),
        ]

    @pytest.mark.parametrize(
        "body",
        [{"id": "not-a-uuid", "key": ""}, {"key": ""}, {"id": f"{KEY_ID}/../x"}, "not json"],
        ids=["bad-id", "no-id", "path-id", "non-json"],
    )
    def test_no_delete_is_sent_without_a_uuid_id(self, miris_api, body):
        miris_api.responses.append((200, body))
        with pytest.raises(MirisIntegrationError, match="no usable key"):
            mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert [c["method"] for c in miris_api.calls] == ["POST"]

    @pytest.mark.parametrize(
        "delete_outcome",
        [(500, RESPONSE_BODY), requests.ConnectionError(RESPONSE_BODY)],
        ids=["delete-500", "delete-transport"],
    )
    def test_a_failed_delete_is_logged_by_type_and_keeps_the_create_error(
            self, miris_api, log_recorder, caplog, delete_outcome):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([(200, {"id": KEY_ID}), delete_outcome])
        with pytest.raises(MirisIntegrationError, match="no usable key") as raised:
            mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert raised.value.status_code == 200
        assert raised.value.__context__ is None
        assert len(miris_api.calls) == 2

        assert (f"Could not delete Miris viewer key {KEY_ID}, returned without usable key material"
                " (MirisIntegrationError)") in log_recorder.text()
        for text in (log_recorder.text(), caplog.text):
            assert RESPONSE_BODY not in text
            assert miris_api.integration_key not in text

    @pytest.mark.parametrize("status", [400, 404, 409, 429, 500, 503])
    def test_a_non_2xx_raises_with_the_status_code_only(self, miris_api, status):
        miris_api.responses.append((status, RESPONSE_BODY))
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        assert raised.value.status_code == status
        assert RESPONSE_BODY not in str(raised.value)

    @pytest.mark.parametrize("value", ["", "not-a-uuid", f"{ASSET_UUID}/../x", None])
    def test_a_non_uuid_asset_raises_without_a_request(self, miris_api, value):
        with pytest.raises(MirisIntegrationError, match="not a UUID"):
            mirisIntegration.create_viewer_key(value, EXPIRES_AT, "n")
        assert miris_api.calls == []

    def test_neither_key_reaches_the_logs(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([(200, _created()), (401, RESPONSE_BODY), (200, _created())])

        mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")

        # Control: the create and the 401 retry were logged.
        assert any(f"Created Miris viewer key {KEY_ID}" in line for line in log_recorder.lines)
        assert any("401" in line for line in log_recorder.lines)
        for text in (log_recorder.text(), caplog.text):
            assert VIEWER_KEY_MATERIAL not in text
            assert miris_api.integration_key not in text
            assert RESPONSE_BODY not in text


VAMS_ASSET_ID = "asset-1"
VAMS_DATABASE_ID = "db-1"
BINDING_TAG = f"vams-asset-{VAMS_ASSET_ID}"
DATABASE_TAG = f"vams-database-{VAMS_DATABASE_ID}"
VIEWER_KEY = "static-viewer-key-do-not-log"


class TestGetAsset:
    def test_sends_one_get_and_returns_the_state_and_tags(self, miris_api):
        miris_api.responses.append(
            (200, {"id": ASSET_UUID, "state": "streamable", "tags": ["vams", BINDING_TAG]})
        )

        asset = mirisIntegration.get_asset(ASSET_UUID)

        assert asset == ("streamable", ("vams", BINDING_TAG))
        state, tags = asset
        assert state == "streamable"
        assert tags == ("vams", BINDING_TAG)
        assert len(miris_api.calls) == 1
        call = miris_api.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{miris_api.base_url}/v1/asset/{ASSET_UUID}"
        assert call["headers"]["Miris-API-Key"] == miris_api.integration_key
        assert call["timeout"] == mirisIntegration._REQUEST_TIMEOUT_SECONDS

    def test_404_returns_none(self, miris_api):
        miris_api.responses.append(404)
        assert mirisIntegration.get_asset(ASSET_UUID) is None

    @pytest.mark.parametrize(
        "body", [{"state": "preview", "tags": None}, {"state": "preview"}], ids=["null", "absent"]
    )
    def test_null_or_absent_tags_are_no_tags(self, miris_api, body):
        miris_api.responses.append((200, body))
        assert mirisIntegration.get_asset(ASSET_UUID) == ("preview", ())

    def test_non_string_tags_are_dropped(self, miris_api):
        miris_api.responses.append((200, {"state": "preview", "tags": [BINDING_TAG, 7, None]}))
        assert mirisIntegration.get_asset(ASSET_UUID).tags == (BINDING_TAG,)

    def test_tags_that_are_not_a_list_raise(self, miris_api):
        miris_api.responses.append((200, {"state": "preview", "tags": BINDING_TAG}))
        with pytest.raises(MirisIntegrationError, match="no tag list"):
            mirisIntegration.get_asset(ASSET_UUID)

    @pytest.mark.parametrize("status", [400, 409, 500, 503])
    def test_other_statuses_raise(self, miris_api, status):
        miris_api.responses.append((status, RESPONSE_BODY))
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.get_asset(ASSET_UUID)
        assert raised.value.status_code == status
        assert RESPONSE_BODY not in str(raised.value)

    @pytest.mark.parametrize(
        "body", [{"id": ASSET_UUID}, {"state": 3}, [], "not json"], ids=["no-state", "non-string", "list", "text"]
    )
    def test_a_response_without_a_string_state_raises(self, miris_api, body):
        miris_api.responses.append((200, body))
        with pytest.raises(MirisIntegrationError, match="no state"):
            mirisIntegration.get_asset(ASSET_UUID)

    def test_a_non_uuid_raises_without_a_request(self, miris_api):
        with pytest.raises(MirisIntegrationError, match="not a UUID"):
            mirisIntegration.get_asset("../v1/asset-viewer-key")
        assert miris_api.calls == []


class TestBinding:
    def test_the_binding_tags_name_the_vams_asset_and_its_database(self):
        assert mirisIntegration.binding_tags(VAMS_DATABASE_ID, VAMS_ASSET_ID) == (BINDING_TAG, DATABASE_TAG)

    def test_bound_with_both_exact_tags(self):
        assert mirisIntegration.is_bound_to_asset(("vams", BINDING_TAG, DATABASE_TAG), VAMS_DATABASE_ID, VAMS_ASSET_ID)

    @pytest.mark.parametrize(
        "tags",
        [
            (),
            ("vams",),
            (BINDING_TAG,),
            (DATABASE_TAG,),
            (BINDING_TAG, "vams-database-db-2"),
            ("vams-asset-asset-2", DATABASE_TAG),
            (f"{BINDING_TAG}x", DATABASE_TAG),
            (BINDING_TAG, f"{DATABASE_TAG}x"),
            (BINDING_TAG.upper(), DATABASE_TAG),
        ],
        ids=["none", "vams-only", "asset-only", "database-only", "other-database", "other-asset",
             "asset-suffix", "database-suffix", "case"],
    )
    def test_not_bound_without_both_exact_tags(self, tags):
        assert not mirisIntegration.is_bound_to_asset(tags, VAMS_DATABASE_ID, VAMS_ASSET_ID)


class TestGetViewerAssetState:
    def test_sends_one_get_with_the_viewer_key_header_and_the_shared_timeout(self, miris_api):
        miris_api.responses.append((200, {"id": ASSET_UUID, "state": "streamable"}))

        assert mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY) == "streamable"

        assert len(miris_api.calls) == 1
        call = miris_api.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{miris_api.base_url}/viewer/v1/asset/{ASSET_UUID}"
        assert call["headers"]["Miris-Viewer-Key"] == VIEWER_KEY
        assert "Miris-API-Key" not in call["headers"]
        assert VIEWER_KEY not in call["url"]
        assert call["timeout"] == mirisIntegration._REQUEST_TIMEOUT_SECONDS
        miris_api.secretsmanager.get_secret_value.assert_not_called()

    def test_404_returns_none(self, miris_api):
        miris_api.responses.append(404)
        assert mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY) is None

    @pytest.mark.parametrize("status", [400, 401, 403, 500])
    def test_other_statuses_raise_with_the_status_code_only(self, miris_api, status):
        miris_api.responses.append((status, RESPONSE_BODY))
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)
        assert raised.value.status_code == status
        assert RESPONSE_BODY not in str(raised.value)
        assert len(miris_api.calls) == 1

    @pytest.mark.parametrize("body", [{"name": "x"}, RESPONSE_BODY], ids=["no-state", "non-json"])
    def test_a_response_without_a_string_state_raises(self, miris_api, body):
        miris_api.responses.append((200, body))
        with pytest.raises(MirisIntegrationError, match="no state") as raised:
            mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)
        assert RESPONSE_BODY not in str(raised.value)

    @pytest.mark.parametrize("viewer_key", ["", None])
    def test_no_viewer_key_raises_without_a_request(self, miris_api, viewer_key):
        with pytest.raises(MirisIntegrationError, match="viewer key"):
            mirisIntegration.get_viewer_asset_state(ASSET_UUID, viewer_key)
        assert miris_api.calls == []

    def test_a_non_uuid_raises_without_a_request(self, miris_api):
        with pytest.raises(MirisIntegrationError, match="not a UUID"):
            mirisIntegration.get_viewer_asset_state("../x", VIEWER_KEY)
        assert miris_api.calls == []

    def test_no_response_body_or_key_reaches_the_logs(self, miris_api, log_recorder, caplog):
        caplog.set_level(logging.DEBUG)
        miris_api.responses.extend([
            (500, RESPONSE_BODY),
            (200, RESPONSE_BODY),
            requests.ConnectionError(f"{RESPONSE_BODY} {VIEWER_KEY}"),
        ])
        for _ in range(3):
            with pytest.raises(MirisIntegrationError):
                mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)

        assert len(miris_api.calls) == 3
        for text in (log_recorder.text(), caplog.text):
            assert RESPONSE_BODY not in text
            assert VIEWER_KEY not in text


class TestRequestDeadline:
    def test_a_long_running_lambda_is_held_to_the_request_budget(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=300000))
        assert deadline == 1000.0 + mirisIntegration._REQUEST_BUDGET_SECONDS

    def test_the_deadline_keeps_a_margin_before_the_lambda_timeout(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=10000))
        assert deadline == 1000.0 + 10 - mirisIntegration._DEADLINE_MARGIN_SECONDS

    def test_no_context_gets_the_request_budget(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        assert mirisIntegration.request_deadline(None) == 1000.0 + mirisIntegration._REQUEST_BUDGET_SECONDS

    def test_a_passed_deadline_raises_without_a_request(self, miris_api):
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=1000))
        with pytest.raises(mirisIntegration.MirisDeadlineExceeded):
            mirisIntegration.get_asset(ASSET_UUID, deadline=deadline)
        assert miris_api.calls == []
        assert issubclass(mirisIntegration.MirisDeadlineExceeded, MirisIntegrationError)

    @pytest.mark.parametrize(
        "remaining, expected",
        [(60, (5, 15)), (20, (5, 15)), (16, (4, 12)), (8, (2, 6)), (1, (0.25, 0.75))],
    )
    def test_connect_plus_read_never_exceeds_the_time_left(self, monkeypatch, remaining, expected):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        timeout = mirisIntegration._call_timeout(1000.0 + remaining)
        assert timeout == pytest.approx(expected)
        assert sum(timeout) <= remaining

    def test_less_than_the_minimum_call_time_raises(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        with pytest.raises(mirisIntegration.MirisDeadlineExceeded):
            mirisIntegration._call_timeout(1000.0 + 0.9)

    def test_the_request_timeout_is_cut_to_the_time_left(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        miris_api.responses.append((200, {"state": "preview", "tags": []}))
        mirisIntegration.get_asset(ASSET_UUID, deadline=1000.0 + 8)
        assert miris_api.calls[0]["timeout"] == pytest.approx((2, 6))

    def test_the_secret_is_not_read_unless_the_bounded_read_and_a_call_fit(self, miris_api, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        needed = mirisIntegration._SECRET_READ_BOUND_SECONDS + mirisIntegration._MIN_CALL_SECONDS
        with pytest.raises(mirisIntegration.MirisDeadlineExceeded):
            mirisIntegration.get_asset(ASSET_UUID, deadline=1000.0 + needed - 0.1)
        miris_api.secretsmanager.get_secret_value.assert_not_called()
        assert miris_api.calls == []

    def test_a_cached_key_needs_no_time_for_the_secret_read(self, miris_api, monkeypatch):
        miris_api.responses.extend([(200, {"state": "preview", "tags": []})] * 2)
        mirisIntegration.get_asset(ASSET_UUID)
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        mirisIntegration.get_asset(ASSET_UUID, deadline=1000.0 + 2)
        assert len(miris_api.calls) == 2
        miris_api.secretsmanager.get_secret_value.assert_called_once()

    def test_the_secrets_manager_client_is_bounded(self):
        import boto3

        client = boto3.client(
            "secretsmanager", region_name="us-east-1", config=mirisIntegration._secrets_config
        )
        resolved = client.meta.config
        assert resolved.connect_timeout == 1
        assert resolved.read_timeout == 2
        # Two attempts in total: the first and one retry.
        assert resolved.retries["total_max_attempts"] == 2
        assert resolved.retries["mode"] == "standard"
        # 2 x (1 s connect + 2 s read) + at most 1 s of backoff before the retry.
        assert mirisIntegration._SECRET_READ_BOUND_SECONDS == 7

    def test_an_api_gateway_receipt_long_ago_shortens_the_deadline(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = {"requestContext": {"requestTimeEpoch": int((5000.0 - 20) * 1000)}}
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=30000), event)
        # 29 s - 20 s elapsed - 3 s margin.
        assert deadline == pytest.approx(1000.0 + 6)

    def test_a_rest_proxy_event_receipt_long_ago_shortens_the_deadline(self, monkeypatch):
        from .conftest import rest_event

        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = rest_event("GET", "/addon/miris/asset-status")
        event["requestContext"]["requestTimeEpoch"] = int((5000.0 - 20) * 1000)
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=30000), event)
        assert deadline == pytest.approx(1000.0 + 6)

    def test_an_http_api_receipt_time_is_read_when_there_is_no_rest_one(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = {"requestContext": {"timeEpoch": int((5000.0 - 20) * 1000)}}
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=30000), event)
        assert deadline == pytest.approx(1000.0 + 6)

    def test_the_rest_receipt_time_wins_over_the_http_api_one(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = {"requestContext": {"requestTimeEpoch": int((5000.0 - 20) * 1000),
                                    "timeEpoch": 5000 * 1000}}
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=30000), event)
        assert deadline == pytest.approx(1000.0 + 6)

    def test_a_fresh_receipt_keeps_the_request_budget(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = {"requestContext": {"requestTimeEpoch": 5000 * 1000}}
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=300000), event)
        assert deadline == 1000.0 + mirisIntegration._REQUEST_BUDGET_SECONDS

    def test_the_lambdas_remaining_time_still_caps_a_fresh_receipt(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        event = {"requestContext": {"requestTimeEpoch": 5000 * 1000}}
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=8000), event)
        assert deadline == 1000.0 + 8 - mirisIntegration._DEADLINE_MARGIN_SECONDS

    @pytest.mark.parametrize(
        "event",
        [None, {}, {"requestContext": {}}, {"requestContext": {"requestTimeEpoch": "x"}}]
        + [
            {"requestContext": {field: value}}
            for field in ("requestTimeEpoch", "timeEpoch")
            for value in (-5, 0, float("-inf"), float("inf"), float("nan"), 10**400, True, 5000)
        ],
    )
    def test_without_a_usable_receipt_time_the_budget_applies(self, monkeypatch, event):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(mirisIntegration.time, "time", lambda: 5000.0)
        deadline = mirisIntegration.request_deadline(FakeLambdaContext(remaining_ms=300000), event)
        assert deadline == 1000.0 + mirisIntegration._REQUEST_BUDGET_SECONDS

    def test_the_cleanup_deadline_ignores_the_request_deadline(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        assert mirisIntegration.cleanup_deadline(FakeLambdaContext(remaining_ms=30000)) == (
            1000.0 + mirisIntegration._CLEANUP_GRACE_SECONDS
        )

    def test_the_cleanup_deadline_stays_inside_the_lambdas_remaining_time(self, monkeypatch):
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: 1000.0)
        deadline = mirisIntegration.cleanup_deadline(FakeLambdaContext(remaining_ms=2000))
        assert deadline == 1000.0 + 2 - mirisIntegration._CLEANUP_MARGIN_SECONDS

    def test_an_auth_retry_is_not_sent_once_the_deadline_has_passed(self, miris_api, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(mirisIntegration.time, "monotonic", lambda: clock["now"])
        inner = mirisIntegration._get_session()

        def request_taking_9_5_seconds(method, url, **kwargs):
            clock["now"] += 9.5
            return inner.request(method, url, **kwargs)

        monkeypatch.setattr(
            mirisIntegration, "_get_session", lambda: SimpleNamespace(request=request_taking_9_5_seconds)
        )
        miris_api.responses.extend([401, 204])
        with pytest.raises(mirisIntegration.MirisDeadlineExceeded):
            mirisIntegration.delete_viewer_key(KEY_ID, deadline=1010.0)
        assert len(miris_api.calls) == 1


class TestRedirects:
    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_an_integration_redirect_raises_and_nothing_is_sent_onward(self, miris_api, status):
        miris_api.responses.append((status, RESPONSE_BODY))
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.get_asset(ASSET_UUID)
        assert raised.value.status_code == status
        assert RESPONSE_BODY not in str(raised.value)
        # One request, to Miris only, sent with redirects off.
        assert [c["url"] for c in miris_api.calls] == [f"{miris_api.base_url}/v1/asset/{ASSET_UUID}"]
        assert miris_api.calls[0]["allow_redirects"] is False

    def test_a_viewer_redirect_raises_and_nothing_is_sent_onward(self, miris_api):
        miris_api.responses.append(302)
        with pytest.raises(MirisIntegrationError) as raised:
            mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)
        assert raised.value.status_code == 302
        assert len(miris_api.calls) == 1
        assert miris_api.calls[0]["allow_redirects"] is False

    def test_redirects_are_off_for_every_call(self, miris_api):
        miris_api.responses.extend([204, (200, _created()), (200, {"state": "x", "tags": []}), (200, {"state": "x"})])
        mirisIntegration.delete_viewer_key(KEY_ID)
        mirisIntegration.create_viewer_key(ASSET_UUID, EXPIRES_AT, "n")
        mirisIntegration.get_asset(ASSET_UUID)
        mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)
        assert len(miris_api.calls) == 4
        assert [c["allow_redirects"] for c in miris_api.calls] == [False] * 4

    def test_the_real_session_does_not_follow_a_redirect(self, monkeypatch):
        """Against the real `requests` session: a 302 to another host is not followed, so neither
        key header reaches that host."""
        sent = []

        def fake_send(self, request, **kwargs):
            sent.append(request.url)
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "https://attacker.example/steal"
            response.url = request.url
            response.request = request
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        monkeypatch.setattr(mirisIntegration, "_session", None)
        monkeypatch.setattr(mirisIntegration, "MIRIS_API_BASE_URL", "https://miris.example.test")

        with pytest.raises(MirisIntegrationError):
            mirisIntegration.get_viewer_asset_state(ASSET_UUID, VIEWER_KEY)
        assert sent == [f"https://miris.example.test/viewer/v1/asset/{ASSET_UUID}"]
