#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Miris upload pipeline container entry point.

Reads the pipeline definition (single JSON arg from constructPipeline), lists the
asset under its resolved root, chooses the root USD file (the one layer no other layer in the
asset references, or the single .usdz), runs the Miris upload flow, writes the Miris stream
metadata for that root file to the run's metadata output path, and exits 0/non-0. A Miris asset the
run created is never deleted, whatever the outcome.

The run ends before its deadline (time_budget): it uploads nothing when too little time is left for
the upload, and its wait for Miris processing ends before the deadline does.
"""
import datetime
import hashlib
import json
import os
import sys
import time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from miris_uploader import MirisClient, MirisError, _redact_response
from stream_metadata import (
    build_stream_metadata_body,
    metadata_object_key,
    metadata_output_location,
)
import time_budget
from upload_config import resolve_upload_config
from usd_packager import (
    UnreadableLayerError,
    choose_roots,
    compute_dependencies,
    direct_references,
    local_download_plan,
    package_usdz,
    paths_outside,
    root_candidates,
    scan_references,
    should_skip_packaging,
)
from utils.secrets import get_miris_integration_key

retry_config = Config(retries={"max_attempts": 5, "mode": "adaptive"})

# Exit codes. The pipelineEnd lambda maps each to the failure reason the workflow shows
# (EXIT_CODE_REASONS); an uncaught exception exits 1.
EXIT_USAGE = 2
EXIT_UNSUPPORTED_EXTENSION = 3
EXIT_TOO_LARGE = 4
EXIT_UNRESOLVED_REFERENCES = 5
EXIT_ROOT_NOT_FOUND = 6
EXIT_NO_METADATA_OUTPUT = 7
EXIT_MIRIS_FAILED = 8
EXIT_AMBIGUOUS_ROOT = 10
EXIT_UNREADABLE_LAYER = 11
EXIT_REFERENCE_OUTSIDE_ASSET = 12
EXIT_INPUT_VERSION_MISSING = 13
EXIT_INSUFFICIENT_TIME = 14

WORKDIR = "/workdir"

_USD_MEDIA_TYPES = {
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usda",
    ".usdc": "model/vnd.usdc",
    ".usdz": "model/vnd.usdz+zip",
}


def _log(msg, **fields):
    """One-line JSON-ish log so CloudWatch keeps it together."""
    payload = {"msg": msg, **fields}
    print(json.dumps(payload, default=str), flush=True)


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stripped_upload_path(upload_path: str) -> tuple[str, str]:
    """Parse 's3://bucket/prefix' → ('bucket', 'prefix') (no trailing /)."""
    assert upload_path.startswith("s3://"), upload_path
    rest = upload_path[5:]
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.rstrip("/")


def _download_version(s3, bucket: str, key: str, local: str, version_id: str) -> None:
    """Download one object, pinned to version_id when the bucket reports one."""
    extra_args = {"VersionId": version_id} if version_id else None
    s3.download_file(bucket, key, local, ExtraArgs=extra_args)


def asset_root_location(stage) -> tuple[str, str]:
    """(bucket, prefix ending in '/') of the asset root; raises ValueError when there is none."""
    location = stage.get("assetRoot") or {}
    bucket = location.get("bucketName") or ""
    prefix = (location.get("objectDir") or "").lstrip("/")
    if not bucket or not prefix.strip("/"):
        raise ValueError("Pipeline definition has no asset root (assetRoot)")
    if not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def list_asset_files(s3, bucket: str, root_prefix: str) -> list[tuple[str, str, int]]:
    """(key, asset-relative path, size) of every file under the asset root."""
    paginator = s3.get_paginator("list_objects_v2")
    sizes = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=root_prefix):
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = int(obj.get("Size", 0))
    return [(key, rel, sizes[key]) for key, rel in local_download_plan(sizes, root_prefix)]


class InputVersionMissingError(Exception):
    """The selected file's launch version no longer exists."""

    def __init__(self, rel: str, version_id: str, error: Exception):
        super().__init__(f"{rel} has no version {version_id}: {error}")
        self.rel, self.version_id, self.error = rel, version_id, error


_MISSING_VERSION_CODES = {"NoSuchVersion", "NoSuchKey", "404", "405", "InvalidArgument"}


def download_files(s3, bucket: str, files, dest_dir: str, pinned, launch_versions=None) -> dict:
    """Download `files` into dest_dir, preserving the relative layout so USD relative references
    resolve. A relative path in `launch_versions` is read at that version (the selected file, at
    the version the run recorded at launch); each other relative path in `pinned` is pinned to the
    version it reports now. Returns {relative path: VersionId or ''} for the pinned files."""
    launch_versions = launch_versions or {}
    versions = {}
    for key, rel, _size in files:
        local = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        if launch_versions.get(rel):
            try:
                _download_version(s3, bucket, key, local, launch_versions[rel])
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in _MISSING_VERSION_CODES:
                    raise InputVersionMissingError(rel, launch_versions[rel], e) from e
                raise
            if rel in pinned:
                versions[rel] = launch_versions[rel]
        elif rel in pinned:
            head = s3.head_object(Bucket=bucket, Key=key)
            versions[rel] = head.get("VersionId") or ""
            _download_version(s3, bucket, key, local, versions[rel])
        else:
            s3.download_file(bucket, key, local)
    return versions


def exit_unreadable_layer(error: UnreadableLayerError, asset_dir: str):
    """Log the unreadable layer by its asset-relative path and exit with EXIT_UNREADABLE_LAYER."""
    base = os.path.realpath(asset_dir)
    relative = os.path.relpath(os.path.realpath(error.path), base).replace(os.sep, "/")
    detail = str(error.error).replace(base + os.sep, "").replace(asset_dir.rstrip("/") + "/", "")
    _log("unreadable_layer", file=relative, error=detail[:500])
    sys.exit(EXIT_UNREADABLE_LAYER)


def _put_artifact(start, upload_local, upload_filename, content_type):
    """SigV4 PUT of the artifact to the temporary upload location POST /v1/content returned.

    The object key MUST match the declared content_path verbatim: Miris resolves the asset's
    materials and textures by content_path, so a percent-encoded key (e.g. "Coral%20House.usdz" for
    content_path "Coral House.usdz") makes Miris extract geometry but silently drop materials.
    boto3 handles wire-level encoding; the logical Key stays raw.
    """
    temp_bucket, temp_prefix = _stripped_upload_path(start["upload_path"])
    temp_key = f"{temp_prefix}/{upload_filename}"
    temp_s3 = boto3.client(
        "s3",
        endpoint_url=start["endpoint_url"],
        region_name=start["region"],
        aws_access_key_id=start["access_key_id"],
        aws_secret_access_key=start["secret_key"],
        aws_session_token=start["session_token"],
        config=retry_config,
    )
    with open(upload_local, "rb") as f:
        temp_s3.put_object(
            Bucket=temp_bucket,
            Key=temp_key,
            Body=f,
            ContentType=content_type,
        )
    _log("sigv4_put_complete", bucket=temp_bucket, key=temp_key)


class InsufficientTimeError(Exception):
    """Too little of the run's time is left to upload the artifact; nothing was sent to Miris."""

    def __init__(self, seconds_left: float, seconds_needed: float):
        super().__init__(f"{int(seconds_left)}s left, {int(seconds_needed)}s needed for the upload")
        self.seconds_left = seconds_left
        self.seconds_needed = seconds_needed


def run_miris_upload(client, *, name, tags, upload_local, upload_filename, content_type,
                     size, task_timeout, write_metadata, deadline=None, clock=time.time):
    """Create the Miris asset, upload the artifact, record it on the file and wait for processing.

    The stream metadata is written as soon as Miris has the upload, so the file is linked to the
    asset whether or not processing finishes within the wait; a wait that runs out ends the run
    with the asset still processing and its streamable promotion skipped. A failure after the asset
    exists keeps the asset in Miris and logs its id (miris_asset_kept). Raises
    InsufficientTimeError, before anything reaches Miris, when the time left before `deadline`
    does not cover the upload. Returns the Miris asset id.
    """
    left = time_budget.seconds_left(deadline, clock())
    needed = time_budget.upload_seconds_needed(size)
    if left is not None and left < needed:
        raise InsufficientTimeError(left, needed)

    start = client.start_upload(
        name=name,
        content_path=upload_filename,
        total_bytes=size,
        tags=tags,
    )
    _log("start_upload", resp=_redact_response(start))
    asset_uuid = start["id"]
    try:
        _put_artifact(start, upload_local, upload_filename, content_type)

        client.mark_upload_complete(asset_uuid)
        _log("upload_marked_complete", asset_uuid=asset_uuid)

        write_metadata(asset_uuid)

        wait = task_timeout
        left = time_budget.seconds_left(deadline, clock())
        if left is not None:
            wait = max(0, min(task_timeout, int(left)))
        try:
            ready = client.poll_until_terminal(asset_uuid, timeout_seconds=wait)
        except TimeoutError as e:
            _log("processing_unfinished", asset_uuid=asset_uuid, error=str(e))
            _log("generate_skipped", reason="processing_unfinished")
            return asset_uuid
        _log("terminal_state_reached", state=ready.get("state"))

        # Streamable promotion is best effort; manual promotion in the Miris Portal is the fallback.
        gen = client.trigger_generate_best_effort(asset_uuid)
        if gen is None:
            _log("generate_skipped", reason="endpoint_unavailable_or_failed")
        else:
            _log("generate_triggered", state=gen.get("state"))
    except BaseException as e:
        _log("miris_asset_kept", asset_uuid=asset_uuid, error=str(e))
        raise
    return asset_uuid


def upload_or_exit(client, **upload_args):
    """Run the upload, exiting with the code for a Miris failure or for too little time left to
    upload. Any other failure propagates and exits 1."""
    try:
        return run_miris_upload(client, **upload_args)
    except InsufficientTimeError as e:
        _log("insufficient_time", seconds_left=int(e.seconds_left),
             seconds_needed=int(e.seconds_needed))
        sys.exit(EXIT_INSUFFICIENT_TIME)
    except MirisError as e:
        _log("miris_upload_failed", error=str(e))
        sys.exit(EXIT_MIRIS_FAILED)


def main():
    deadline = time_budget.run_deadline(os.environ, time.time())
    if len(sys.argv) < 2:
        _log("usage: __main__.py <definition-json>")
        sys.exit(EXIT_USAGE)
    definition = json.loads(sys.argv[1])
    stage = definition["stages"][0]
    asset_id = stage.get("assetId", "")
    database_id = stage.get("databaseId", "")
    trigger = stage["triggerInput"]
    miris_base = stage["mirisApiBaseUrl"]
    secret_arn = stage["mirisApiKeySecretArn"]
    task_timeout = int(stage["taskTimeoutSeconds"])
    max_bytes = int(stage["maxAssetSizeBytes"])

    # Both identities become the Miris asset's binding tags.
    if not asset_id or not database_id:
        _log("asset_identity_missing", asset_id=asset_id, database_id=database_id)
        sys.exit(EXIT_USAGE)
    try:
        asset_bucket, asset_root = asset_root_location(stage)
    except ValueError as e:
        _log("asset_root_missing", error=str(e))
        sys.exit(EXIT_USAGE)

    trigger_key = trigger["objectKey"]
    extension = trigger["fileExtension"].lower()
    if extension not in _USD_MEDIA_TYPES:
        _log("unsupported_extension", extension=extension)
        sys.exit(EXIT_UNSUPPORTED_EXTENSION)

    # The stream metadata is the pipeline's only output, so a run without somewhere
    # to write it fails before anything is uploaded to Miris.
    try:
        metadata_bucket, metadata_prefix = metadata_output_location(stage)
    except ValueError as e:
        _log("metadata_output_path_missing", error=str(e))
        sys.exit(EXIT_NO_METADATA_OUTPUT)

    s3 = boto3.client("s3", config=retry_config)
    os.makedirs(WORKDIR, exist_ok=True)
    asset_dir = os.path.join(WORKDIR, "asset")

    # The root is chosen from the asset's USD files, whichever of them triggered this run.
    extensions = [
        e for e in (stage.get("rootExtensions") or _USD_MEDIA_TYPES) if e in _USD_MEDIA_TYPES]
    files = list_asset_files(s3, asset_bucket, asset_root)
    candidates = root_candidates([rel for _, rel, _ in files], extensions)
    if not candidates:
        _log("root_usd_not_found", asset_root=asset_root, trigger_key=trigger_key,
             listed_files=len(files))
        sys.exit(EXIT_ROOT_NOT_FOUND)

    # A .usdz references no other file in the asset, so several of them and no other layer are
    # several roots; that is known before anything downloads.
    if len(candidates) > 1 and all(c.lower().endswith(".usdz") for c in candidates):
        _log("ambiguous_root", reason="ambiguous_root", roots=candidates[:20])
        sys.exit(EXIT_AMBIGUOUS_ROOT)

    # A lone .usdz is self-contained; anything else needs the whole asset on disk.
    if len(candidates) == 1 and candidates[0].lower().endswith(".usdz"):
        to_download = [f for f in files if f[1] == candidates[0]]
    else:
        to_download = files
    total = sum(size for _, _, size in to_download)
    if total > max_bytes:
        _log("file_too_large", size=total, max_bytes=max_bytes)
        sys.exit(EXIT_TOO_LARGE)

    # The selected file is read at the version the run recorded at launch; the other layers are
    # read at their current version.
    trigger_rel = trigger_key[len(asset_root):] if trigger_key.startswith(asset_root) else ""
    launch_versions = {trigger_rel: trigger.get("versionId") or ""} if trigger_rel else {}
    try:
        versions = download_files(s3, asset_bucket, to_download, asset_dir, set(candidates),
                                  launch_versions)
    except InputVersionMissingError as e:
        _log("input_version_missing", file=e.rel, version_id=e.version_id, error=str(e.error))
        sys.exit(EXIT_INPUT_VERSION_MISSING)

    def references_of(rel):
        return direct_references(os.path.join(asset_dir, rel), asset_dir)

    try:
        roots = choose_roots(candidates, references_of)
    except UnreadableLayerError as e:
        exit_unreadable_layer(e, asset_dir)
    if len(roots) > 1:
        _log("ambiguous_root", reason="ambiguous_root", roots=roots[:20])
        sys.exit(EXIT_AMBIGUOUS_ROOT)
    if not roots:
        _log("root_usd_not_found", reason="every_layer_is_referenced", candidates=candidates[:20])
        sys.exit(EXIT_ROOT_NOT_FOUND)
    root_rel = roots[0]
    root_local = os.path.join(asset_dir, root_rel)
    source_version_id = versions.get(root_rel, "")
    filename = os.path.basename(root_rel)
    root_extension = os.path.splitext(filename)[1].lower()
    name_no_ext = os.path.splitext(filename)[0]
    _log("root_chosen", root=root_rel, candidates=len(candidates), trigger_key=trigger_key)

    # Resolve the single self-contained artifact to upload to Miris.
    if root_extension == ".usdz":
        upload_local = root_local
        upload_filename = filename
        upload_content_type = _USD_MEDIA_TYPES[".usdz"]
    else:
        try:
            layers, assets, unresolved = compute_dependencies(root_local)
        except UnreadableLayerError as e:
            # Name the layer itself when the root's own references lead to it.
            try:
                scan_references([root_rel], references_of)
            except UnreadableLayerError as unreadable:
                exit_unreadable_layer(unreadable, asset_dir)
            exit_unreadable_layer(e, asset_dir)
        if unresolved:
            _log("unresolved_references", count=len(unresolved), paths=unresolved[:20])
            sys.exit(EXIT_UNRESOLVED_REFERENCES)
        # Only files the asset holds are packaged, never one a reference resolves to elsewhere.
        outside = paths_outside(layers + assets, asset_dir)
        if outside:
            _log("reference_outside_asset", count=len(outside), paths=outside[:20])
            sys.exit(EXIT_REFERENCE_OUTSIDE_ASSET)
        if should_skip_packaging(len(layers), len(assets)):
            _log("packaging_skipped", reason="no_external_dependencies")
            upload_local = root_local
            upload_filename = filename
            upload_content_type = _USD_MEDIA_TYPES[root_extension]
        else:
            upload_local = os.path.join(WORKDIR, f"{name_no_ext}.usdz")
            package_usdz(root_local, upload_local)
            upload_filename = f"{name_no_ext}.usdz"
            upload_content_type = _USD_MEDIA_TYPES[".usdz"]
            _log(
                "packaged_usdz",
                file=upload_filename,
                layers=len(layers),
                assets=len(assets),
            )

    size = os.path.getsize(upload_local)
    sha = _sha256_of_file(upload_local)
    _log("artifact_ready", path=upload_local, size=size, sha256=sha[:16])

    # Read the already-rendered per-run configuration (template values substituted by the
    # workflow before this container started). Never fatal: every field falls back to a
    # VAMS-derived default.
    cfg = resolve_upload_config(stage, upload_filename, s3)
    _log("upload_config_resolved", asset_name=cfg.asset_name, tags=cfg.tags)

    # Secrets Manager
    key = get_miris_integration_key(secret_arn, region_name=os.environ.get("AWS_REGION"))
    client = MirisClient(miris_base, key)

    miris_tags = ["vams", f"vams-asset-{asset_id}", f"vams-database-{database_id}"] + cfg.tags

    def write_metadata(asset_uuid):
        """Write the stream metadata for the root USD file."""
        body = build_stream_metadata_body(
            asset_uuid=asset_uuid,
            source_version_id=source_version_id,
            display_name=cfg.asset_name,
            uploaded_at=datetime.datetime.now(datetime.timezone.utc),
        )
        metadata_key = metadata_object_key(metadata_prefix, root_rel)
        s3.put_object(
            Bucket=metadata_bucket,
            Key=metadata_key,
            Body=json.dumps(body, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        _log(
            "stream_metadata_written",
            bucket=metadata_bucket,
            key=metadata_key,
            asset_uuid=asset_uuid,
            source_version_id=source_version_id,
        )

    upload_or_exit(
        client,
        name=cfg.asset_name,
        tags=miris_tags,
        upload_local=upload_local,
        upload_filename=upload_filename,
        content_type=upload_content_type,
        size=size,
        task_timeout=task_timeout,
        write_metadata=write_metadata,
        deadline=deadline,
    )


if __name__ == "__main__":
    main()
