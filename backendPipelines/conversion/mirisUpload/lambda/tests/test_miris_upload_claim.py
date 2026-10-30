#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""The gate and every release site derive the claim key here, so they cannot drift apart."""
import io
import json
from unittest.mock import MagicMock

import pytest

import mirisClaim
from miris_upload_fakes import FakeS3

pytestmark = pytest.mark.unit


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


# The workflow supplies `auxBucket + pipelines/{pipelineName}/{executionId}/`.
EXEC_A = "s3://vams-aux-bucket/pipelines/miris-upload/exec-1111/"
EXEC_B = "s3://vams-aux-bucket/pipelines/miris-upload/exec-2222/"
FP = "f" * 64
USD_EXTENSIONS = [".usd", ".usda", ".usdc", ".usdz"]


def test_key_is_bucket_root_scoped_and_execution_independent():
    assert mirisClaim.claim_location(EXEC_A, "db1", "a1", FP) == (
        "vams-aux-bucket", f"locks/miris-upload/db1/a1/{FP}.claim")
    assert mirisClaim.claim_location(EXEC_B, "db1", "a1", FP) == (
        "vams-aux-bucket", f"locks/miris-upload/db1/a1/{FP}.claim")


def test_bare_bucket_uri_is_accepted():
    assert mirisClaim.claim_location("s3://vams-aux-bucket", "db1", "a1", FP) == (
        "vams-aux-bucket", f"locks/miris-upload/db1/a1/{FP}.claim")


@pytest.mark.parametrize("aux,database,asset,fingerprint", [
    ("", "db1", "a1", FP), (EXEC_A, "", "a1", FP), (EXEC_A, "db1", "", FP), (EXEC_A, "db1", "a1", ""),
    ("", "", "", "")])
def test_missing_inputs_yield_no_location(aux, database, asset, fingerprint):
    assert mirisClaim.claim_location(aux, database, asset, fingerprint) == (None, None)


def test_the_same_asset_id_in_another_database_has_another_key():
    assert mirisClaim.claim_location(EXEC_A, "db1", "a1", FP) != mirisClaim.claim_location(
        EXEC_A, "db2", "a1", FP)


# --- content fingerprint ----------------------------------------------------------------------


def _fingerprint(s3, root="a1/"):
    return mirisClaim.content_fingerprint(
        mirisClaim.list_trigger_files(s3, "assets", root, USD_EXTENSIONS))


def _asset(**files):
    """An asset bucket holding `files` ({relative path: etag}) under the root `a1/`."""
    s3 = FakeS3()
    for rel, etag in files.items():
        s3.put_file("assets", f"a1/{rel}", etag)
    return s3


def _layers():
    return {"scene.usda": "e-scene", "geo/geo.usd": "e-geo", "tex/wood.png": "e-wood"}


def test_unchanged_content_has_the_same_fingerprint():
    assert _fingerprint(_asset(**_layers())) == _fingerprint(_asset(**_layers()))


def test_a_changed_layer_etag_changes_the_fingerprint():
    changed = dict(_layers(), **{"geo/geo.usd": "e-geo-2"})
    assert _fingerprint(_asset(**changed)) != _fingerprint(_asset(**_layers()))


def test_an_added_layer_changes_the_fingerprint():
    added = dict(_layers(), **{"mat.usdc": "e-mat"})
    assert _fingerprint(_asset(**added)) != _fingerprint(_asset(**_layers()))


def test_a_changed_non_trigger_file_leaves_the_fingerprint():
    changed = dict(_layers(), **{"tex/wood.png": "e-wood-2", "notes.txt": "e-notes"})
    assert _fingerprint(_asset(**changed)) == _fingerprint(_asset(**_layers()))


def test_the_extension_match_is_exact_and_case_insensitive():
    s3 = _asset(**{"Scene.USDA": "e1", "scene.usda.bak": "e2", "x.us": "e3", "folder/": "e4"})
    files = mirisClaim.list_trigger_files(s3, "assets", "a1/", USD_EXTENSIONS)
    assert files == [("Scene.USDA", '"e1"')]


def test_the_fingerprint_is_keyed_on_the_asset_relative_path():
    """The same layers under a prefixed root fingerprint the same as under a bare one."""
    bare, prefixed = FakeS3(), FakeS3()
    for rel, etag in _layers().items():
        bare.put_file("assets", f"a1/{rel}", etag)
        prefixed.put_file("assets", f"vams/data/a1/{rel}", etag)
    assert _fingerprint(prefixed, "vams/data/a1/") == _fingerprint(bare)
    assert prefixed.list_calls == [("assets", "vams/data/a1/")]


def test_listing_order_does_not_matter():
    pairs = [("b.usd", '"2"'), ("a.usd", '"1"')]
    assert mirisClaim.content_fingerprint(pairs) == mirisClaim.content_fingerprint(pairs[::-1])


def test_no_trigger_files_give_no_fingerprint():
    assert mirisClaim.content_fingerprint([]) == ""


@pytest.mark.parametrize("root,expected", [
    ("a1/", "a1/"), ("a1", "a1/"), ("/vams/data/a1", "vams/data/a1/"), ("", ""), (None, "")])
def test_asset_root_prefix_normalizes_to_a_folder(root, expected):
    assert mirisClaim.asset_root_prefix(root) == expected


# --- release ----------------------------------------------------------------------------------


def _held_by(token):
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **_: {
        "Body": io.BytesIO(json.dumps({"owner": mirisClaim.owner_id(token)}).encode())}
    return s3


def test_release_deletes_the_claim_this_execution_holds():
    s3 = _held_by("tok-a")
    assert mirisClaim.release_claim(
        s3, _Logger(), EXEC_A, "db1", "a1", FP, mirisClaim.owner_id("tok-a")) is True
    s3.delete_object.assert_called_once_with(
        Bucket="vams-aux-bucket", Key=f"locks/miris-upload/db1/a1/{FP}.claim")


def test_release_without_a_location_is_a_no_op():
    s3 = MagicMock()
    assert mirisClaim.release_claim(s3, _Logger(), "", "db1", "a1", FP, "owner") is False
    s3.delete_object.assert_not_called()


def test_release_without_a_fingerprint_is_a_no_op():
    """A manual run writes no claim, so it has nothing to release."""
    s3 = MagicMock()
    assert mirisClaim.release_claim(s3, _Logger(), EXEC_A, "db1", "a1", "", "owner") is False
    s3.get_object.assert_not_called()
    s3.delete_object.assert_not_called()


def test_release_swallows_delete_errors():
    s3 = _held_by("tok-a")
    s3.delete_object.side_effect = Exception("AccessDenied")
    assert mirisClaim.release_claim(
        s3, _Logger(), EXEC_A, "db1", "a1", FP, mirisClaim.owner_id("tok-a")) is False


def test_release_leaves_a_claim_held_by_another_execution():
    s3 = _held_by("tok-b")
    assert mirisClaim.release_claim(
        s3, _Logger(), EXEC_A, "db1", "a1", FP, mirisClaim.owner_id("tok-a")) is False
    s3.delete_object.assert_not_called()


@pytest.mark.parametrize("body", [b"not json", b"[]"])
def test_release_leaves_an_unreadable_claim(body):
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **_: {"Body": io.BytesIO(body)}
    assert mirisClaim.release_claim(s3, _Logger(), EXEC_A, "db1", "a1", FP, "") is False
    s3.delete_object.assert_not_called()


def test_owner_id_is_a_digest_and_empty_without_a_token():
    assert mirisClaim.owner_id("") == ""
    assert len(mirisClaim.owner_id("tok-a")) == 64
    assert "tok-a" not in mirisClaim.owner_id("tok-a")
