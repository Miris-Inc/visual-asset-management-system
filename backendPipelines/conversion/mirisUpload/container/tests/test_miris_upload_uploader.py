#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import json
import os
import sys

import pytest
import responses

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = "https://app.miris.test"
KEY = "test-integration-key-xxx"
UUID = "11111111-2222-3333-4444-555555555555"


@responses.activate
def test_start_upload_posts_correct_body_and_returns_response():
    from miris_uploader import MirisClient

    responses.add(
        responses.POST,
        f"{BASE}/v1/content",
        json={
            "id": UUID,
            "endpoint_type": "s3",
            "endpoint_url": "https://s3.us-west-1.amazonaws.com",
            "region": "us-west-1",
            "access_key_id": "AK",
            "secret_key": "SK",
            "session_token": "ST",
            "upload_path": "s3://miris-uploads-bucket/ws/up",
        },
        status=200,
    )

    client = MirisClient(BASE, KEY)
    resp = client.start_upload(
        name="model",
        content_path="model.usdz",
        total_bytes=1024,
        tags=["vams"],
    )
    sent = json.loads(responses.calls[0].request.body)
    assert sent == {
        "name": "model",
        "content_path": "model.usdz",
        "file_count": 1,
        "input_type": "usd",
        "output_type": "static",
        "tags": ["vams"],
        "total_bytes": 1024,
    }
    assert responses.calls[0].request.headers["Miris-Integration-Key"] == KEY
    assert "Authorization" not in responses.calls[0].request.headers
    assert resp["id"] == UUID


@responses.activate
def test_mark_upload_complete_puts_status_completed_to_content_endpoint():
    from miris_uploader import MirisClient

    responses.add(
        responses.PUT,
        f"{BASE}/v1/content/{UUID}",
        json={"id": UUID, "upload_status": "completed"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    client.mark_upload_complete(UUID)
    sent = json.loads(responses.calls[0].request.body)
    assert sent == {"status": "completed"}


@responses.activate
def test_trigger_generate_best_effort_returns_response_on_2xx():
    from miris_uploader import MirisClient

    responses.add(
        responses.POST,
        f"{BASE}/v1/asset/{UUID}/generate",
        json={"asset_id": UUID, "job_id": "job1", "state": "processing_streamable"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    result = client.trigger_generate_best_effort(UUID)
    assert result["state"] == "processing_streamable"
    body = responses.calls[0].request.body
    assert body in (None, b"", "")


@responses.activate
def test_trigger_generate_best_effort_returns_none_on_4xx():
    from miris_uploader import MirisClient

    responses.add(
        responses.POST,
        f"{BASE}/v1/asset/{UUID}/generate",
        json={"detail": "not found"},
        status=404,
    )

    client = MirisClient(BASE, KEY)
    result = client.trigger_generate_best_effort(UUID)
    assert result is None


@responses.activate
def test_poll_until_terminal_breaks_on_preview_state():
    from miris_uploader import MirisClient

    for state in ("processing_preview", "processing_preview"):
        responses.add(
            responses.GET,
            f"{BASE}/v1/asset/{UUID}",
            json={"id": UUID, "state": state},
            status=200,
        )
    responses.add(
        responses.GET,
        f"{BASE}/v1/asset/{UUID}",
        json={"id": UUID, "state": "preview"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    result = client.poll_until_terminal(
        UUID, timeout_seconds=10, poll_interval_seconds=0
    )
    assert result["state"] == "preview"
    assert len(responses.calls) == 3


@responses.activate
def test_poll_until_terminal_accepts_streamable_as_success():
    from miris_uploader import MirisClient

    responses.add(
        responses.GET,
        f"{BASE}/v1/asset/{UUID}",
        json={"id": UUID, "state": "streamable"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    result = client.poll_until_terminal(
        UUID, timeout_seconds=10, poll_interval_seconds=0
    )
    assert result["state"] == "streamable"


@responses.activate
def test_poll_until_terminal_raises_on_error_state():
    from miris_uploader import MirisClient, MirisError

    responses.add(
        responses.GET,
        f"{BASE}/v1/asset/{UUID}",
        json={"id": UUID, "state": "failed"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    with pytest.raises(MirisError, match="failed"):
        client.poll_until_terminal(
            UUID, timeout_seconds=10, poll_interval_seconds=0
        )


@responses.activate
def test_poll_until_terminal_times_out():
    from miris_uploader import MirisClient

    responses.add(
        responses.GET,
        f"{BASE}/v1/asset/{UUID}",
        json={"id": UUID, "state": "processing_preview"},
        status=200,
    )

    client = MirisClient(BASE, KEY)
    with pytest.raises(TimeoutError):
        client.poll_until_terminal(
            UUID, timeout_seconds=0, poll_interval_seconds=0
        )


def test_redact_response_masks_secret_triple():
    from miris_uploader import _redact_response

    redacted = _redact_response(
        {
            "id": "x",
            "access_key_id": "AK",
            "secret_key": "SK",
            "session_token": "ST",
            "endpoint_url": "https://example.com",
        }
    )
    assert redacted["id"] == "x"
    assert redacted["endpoint_url"] == "https://example.com"
    assert redacted["access_key_id"] == "<redacted>"
    assert redacted["secret_key"] == "<redacted>"
    assert redacted["session_token"] == "<redacted>"


def test_redact_response_is_pure():
    from miris_uploader import _redact_response

    original = {"secret_key": "SK"}
    _redact_response(original)
    assert original == {"secret_key": "SK"}


def _client(**kwargs):
    from miris_uploader import MirisClient

    sleeps = []
    client = MirisClient(BASE, KEY, sleep=sleeps.append, **kwargs)
    return client, sleeps


@responses.activate
def test_get_asset_retries_transient_5xx_with_backoff():
    for status in (502, 503):
        responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={}, status=status)
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"id": UUID, "state": "preview"})

    client, sleeps = _client()
    assert client.get_asset(UUID)["state"] == "preview"
    assert len(responses.calls) == 3
    # Exponential: each wait at least as long as the one before it.
    assert len(sleeps) == 2 and 0 < sleeps[0] <= sleeps[1]


@responses.activate
def test_get_asset_retries_connection_errors():
    import requests

    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}",
                  body=requests.ConnectionError("connection reset"))
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"id": UUID, "state": "preview"})

    client, sleeps = _client()
    assert client.get_asset(UUID)["state"] == "preview"
    assert len(sleeps) == 1


@responses.activate
def test_get_asset_gives_up_after_the_attempt_limit():
    from miris_uploader import MirisError

    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={}, status=503)

    client, sleeps = _client(max_attempts=3)
    with pytest.raises(MirisError, match="503"):
        client.get_asset(UUID)
    assert len(responses.calls) == 3
    assert len(sleeps) == 2


@responses.activate
def test_get_asset_does_not_retry_a_4xx():
    from miris_uploader import MirisError

    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"detail": "nope"}, status=404)

    client, sleeps = _client()
    with pytest.raises(MirisError, match="404"):
        client.get_asset(UUID)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_poll_survives_a_transient_error_between_polls():
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"state": "processing_preview"})
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={}, status=500)
    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", json={"state": "preview"})

    client, _ = _client()
    result = client.poll_until_terminal(UUID, timeout_seconds=10, poll_interval_seconds=0)
    assert result["state"] == "preview"


@responses.activate
def test_mark_upload_complete_retries_transient_5xx():
    responses.add(responses.PUT, f"{BASE}/v1/content/{UUID}", json={}, status=500)
    responses.add(responses.PUT, f"{BASE}/v1/content/{UUID}", json={"id": UUID})

    client, _ = _client()
    client.mark_upload_complete(UUID)
    assert len(responses.calls) == 2


@responses.activate
def test_start_upload_is_not_retried():
    # POST /v1/content creates an asset; a retry after a response was lost would create a second one.
    from miris_uploader import MirisError

    responses.add(responses.POST, f"{BASE}/v1/content", json={}, status=503)

    client, sleeps = _client()
    with pytest.raises(MirisError):
        client.start_upload(name="m", content_path="m.usdz", total_bytes=1, tags=[])
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_trigger_generate_best_effort_returns_none_on_connection_error():
    import requests

    responses.add(responses.POST, f"{BASE}/v1/asset/{UUID}/generate",
                  body=requests.ConnectionError("connection reset"))

    client, _ = _client()
    assert client.trigger_generate_best_effort(UUID) is None


@responses.activate
def test_trigger_generate_best_effort_returns_none_on_5xx_and_unreadable_body():
    responses.add(responses.POST, f"{BASE}/v1/asset/{UUID}/generate", json={}, status=500)
    responses.add(responses.POST, f"{BASE}/v1/asset/{UUID}/generate", body="not json", status=200)

    client, _ = _client()
    assert client.trigger_generate_best_effort(UUID) is None
    assert client.trigger_generate_best_effort(UUID) is None


@responses.activate
@pytest.mark.parametrize("method,call", [
    ("GET", lambda c: c.get_asset(UUID)),
    ("PUT", lambda c: c.mark_upload_complete(UUID)),
])
def test_idempotent_calls_retry_a_response_cut_off_mid_body(method, call):
    import requests

    url = f"{BASE}/v1/content/{UUID}" if method == "PUT" else f"{BASE}/v1/asset/{UUID}"
    responses.add(method, url, body=requests.exceptions.ChunkedEncodingError("connection broken"))
    responses.add(method, url, json={"id": UUID, "state": "preview"}, status=200)

    client, sleeps = _client()
    assert call(client)
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_start_upload_does_not_retry_a_response_cut_off_mid_body():
    import requests
    from miris_uploader import MirisError

    responses.add(responses.POST, f"{BASE}/v1/content",
                  body=requests.exceptions.ChunkedEncodingError("connection broken"))

    client, sleeps = _client()
    with pytest.raises(MirisError):
        client.start_upload(name="m", content_path="m.usdz", total_bytes=1, tags=[])
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_a_malformed_request_is_not_retried():
    import requests
    from miris_uploader import MirisError

    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}",
                  body=requests.exceptions.InvalidURL("bad url"))

    client, sleeps = _client()
    with pytest.raises(MirisError):
        client.get_asset(UUID)
    assert sleeps == []


@responses.activate
def test_an_unreadable_asset_body_is_a_miris_error():
    from miris_uploader import MirisError

    responses.add(responses.GET, f"{BASE}/v1/asset/{UUID}", body="<html>", status=200)

    client, _ = _client()
    with pytest.raises(MirisError, match="unreadable"):
        client.get_asset(UUID)


ELSEWHERE = "https://elsewhere.test/collect"


def _redirect_then_elsewhere(method, url):
    """A 302 from `url` to another host, which also answers, so a followed redirect is recorded."""
    responses.add(method, url, status=302, headers={"Location": ELSEWHERE})
    for other in (responses.GET, responses.POST, responses.PUT):
        responses.add(other, ELSEWHERE, json={"id": UUID, "state": "preview"}, status=200)


@responses.activate
@pytest.mark.parametrize("method,path,call", [
    ("POST", "/v1/content",
     lambda c: c.start_upload(name="m", content_path="m.usdz", total_bytes=1, tags=[])),
    ("PUT", f"/v1/content/{UUID}", lambda c: c.mark_upload_complete(UUID)),
    ("GET", f"/v1/asset/{UUID}", lambda c: c.get_asset(UUID)),
])
def test_a_redirect_fails_the_call_without_a_second_request(method, path, call):
    from miris_uploader import MirisError

    _redirect_then_elsewhere(method, f"{BASE}{path}")

    client, sleeps = _client()
    with pytest.raises(MirisError, match="302"):
        call(client)
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_a_redirect_is_not_followed_by_the_best_effort_generate_call():
    _redirect_then_elsewhere(responses.POST, f"{BASE}/v1/asset/{UUID}/generate")

    client, _ = _client()
    assert client.trigger_generate_best_effort(UUID) is None
    assert len(responses.calls) == 1
    assert all(c.request.url.startswith(BASE) for c in responses.calls)
