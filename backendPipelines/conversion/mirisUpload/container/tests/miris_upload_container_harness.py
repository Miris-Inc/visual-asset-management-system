#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Fakes for running the container entry point: S3, Secrets Manager and Miris over a real working
directory. Shared by the entry-point tests through the `container` fixture in conftest.py."""
import importlib.util
import io
import json
import os
import sys
import types
from unittest.mock import MagicMock

CONTAINER_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, CONTAINER_DIR)

ASSET_ID = "x86487e0b-9f50-4b7b-9b59-8963c6791b56"
METADATA_PREFIX = "pipelines/miris/job/output/E1/metadata/"
MIRIS_ASSET_UUID = "0b6f4c9e-6a43-4bb5-9d0c-4b3e2b1f7a10"


def load_entry_point():
    """The container's __main__.py under a private name (`import __main__` is pytest's own)."""
    spec = importlib.util.spec_from_file_location(
        "miris_upload_container_main", os.path.join(CONTAINER_DIR, "__main__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AssetBucketS3:
    """An in-memory S3 client: `files` maps (bucket, key) to bytes; versions are 'v-<key>'."""

    def __init__(self):
        self.files = {}
        self.unversioned = set()
        self.list_calls = []
        self.downloads = []
        self.put_object = MagicMock(name="put_object")
        self.upload_file = MagicMock(name="upload_file")

    def add(self, bucket, key, body=b"x"):
        self.files[(bucket, key)] = body if isinstance(body, bytes) else body.encode("utf-8")

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                fake.list_calls.append((Bucket, Prefix))
                return [{"Contents": [
                    {"Key": key, "Size": len(body), "ETag": '"e"'}
                    for (bucket, key), body in sorted(fake.files.items())
                    if bucket == Bucket and key.startswith(Prefix)
                ]}]

        return _Paginator()

    def head_object(self, Bucket, Key):
        head = {"ContentLength": len(self.files[(Bucket, Key)])}
        if Key not in self.unversioned:
            head["VersionId"] = f"v-{Key}"
        return head

    def download_file(self, bucket, key, local, ExtraArgs=None):
        self.downloads.append((bucket, key, ExtraArgs))
        with open(local, "wb") as f:
            f.write(self.files[(bucket, key)])

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.files[(Bucket, Key)])}


def definition(asset_root="", trigger_rel="model.usdz", **stage_overrides):
    """A pipeline definition for asset ASSET_ID rooted at `asset_root` (default '{ASSET_ID}/')."""
    root = asset_root or f"{ASSET_ID}/"
    stage = {
        "type": "MIRIS_UPLOAD",
        "assetId": ASSET_ID,
        "databaseId": "db1",
        "triggerInput": {
            "bucketName": "asset-bucket",
            "objectKey": f"{root}{trigger_rel}",
            "fileExtension": os.path.splitext(trigger_rel)[1],
        },
        "assetRoot": {"bucketName": "asset-bucket", "objectDir": root},
        "rootExtensions": [".usd", ".usda", ".usdc", ".usdz"],
        "outputMetadataFiles": {"bucketName": "asset-bucket", "objectDir": METADATA_PREFIX},
        "mirisApiBaseUrl": "https://api.example.test",
        "mirisApiKeySecretArn": "arn:aws:secretsmanager:::secret:x",
        "taskTimeoutSeconds": 60,
        "maxAssetSizeBytes": 1_000_000,
    }
    stage.update(stage_overrides)
    return json.dumps({"jobName": "job-1", "stages": [stage]})


def make_container(monkeypatch, tmp_path):
    """The entry point with its working directory under tmp_path and every remote call faked."""
    main_mod = load_entry_point()
    monkeypatch.setattr(main_mod, "WORKDIR", str(tmp_path / "workdir"))

    s3 = AssetBucketS3()
    temp_s3 = MagicMock(name="temp_s3")
    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = lambda *a, **kw: temp_s3 if "endpoint_url" in kw else s3
    monkeypatch.setattr(main_mod, "boto3", fake_boto3)

    miris = MagicMock(name="miris")
    miris.start_upload.return_value = {
        "id": MIRIS_ASSET_UUID,
        "upload_path": "s3://miris-temp/prefix",
        "endpoint_url": "https://s3.example.test",
        "region": "us-east-1",
        "access_key_id": "a",
        "secret_key": "s",
        "session_token": "t",
    }
    miris.poll_until_terminal.return_value = {"state": "streamable"}
    miris.trigger_generate_best_effort.return_value = None
    miris_cls = MagicMock(return_value=miris)
    monkeypatch.setattr(main_mod, "MirisClient", miris_cls)
    monkeypatch.setattr(main_mod, "get_miris_integration_key", lambda *a, **kw: "key")

    def run(definition_json):
        monkeypatch.setattr(sys, "argv", ["__main__.py", definition_json])
        main_mod.main()

    return types.SimpleNamespace(
        main=main_mod, s3=s3, temp_s3=temp_s3, miris=miris, miris_cls=miris_cls, run=run,
        workdir=tmp_path / "workdir")
