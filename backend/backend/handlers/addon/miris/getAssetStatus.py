# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""GET /addon/miris/asset-status?databaseId=<id>&assetId=<id>&filePath=<path or object key>

Reports the Miris stream recorded on one asset file. The Miris asset UUID is read server side
from the file's metadata (`miris_asset_uuid` in the asset file metadata table, keyed by exactly
this databaseId, assetId and filePath). The request carries no UUID, so a caller can only learn
the status of a stream that the file itself references.

`filePath` is the asset-relative path (`/folder/scene.usd`) or the file's full object key. A
key under the asset's root key (`assetLocation.Key`) or under `{assetId}/` is reduced to the
relative path the metadata service stores the file's metadata under.

Response body:

    filePath         the normalized asset-relative path, e.g. "/scene.usd"
    mirisAssetUuid   `miris_asset_uuid`, or null when it is absent or empty
    sourceVersionId  `miris_source_version_id`, or null when absent ("" when the source
                     version was not known at upload time)
    uploadedAt       `miris_uploaded_at` (ISO 8601), or null when absent
    displayName      `miris_display_name`, or null when absent
    state            one of the states below

States:

    notStreamed    `miris_asset_uuid` is absent or empty. Miris is not called.
    invalidUuid    `miris_asset_uuid` is set but is not a UUID. Miris is not called.
    notBound       Miris has no such asset (404), or the asset lacks either tag that binds it
                   to this VAMS asset, `vams-asset-{assetId}` and `vams-database-{databaseId}`.
    unknown        The processing state could not be established. Either the add-on is not
                   fully configured for its viewer key mode (Miris is not called), or Miris was
                   unreachable, did not answer before the request deadline, returned a non-2xx
                   status (the viewer API's 404 included), or returned a payload without a
                   usable `state`.
    processing     The viewer API answered 400, which it does while Miris prepares the asset
                   (static mode only).
    <Miris state>  The processing state reported by Miris, passed through as is:
                   `preview` while Miris prepares the asset, `streamable` once it can be
                   streamed, and `error` or `failed` for a definitive processing failure.

In both viewer key modes (`MIRIS_VIEWER_KEY_MODE`) the asset and its tags are first read with the
Miris Integration Key (`GET /v1/asset/{uuid}`, see `mirisIntegration`), which establishes the
binding. The Integration Key resolves to its owning Miris user's workspace, which must be the
workspace holding the assets. The processing state then comes from:

  - static (the default): the viewer API, read with the deployment-wide viewer key
    (`MIRIS_VIEWER_KEY`), the same credential the frontend streams with. It sees exactly the
    assets the viewer can stream.
  - perView: the Integration API read above, since no deployment-wide viewer key exists.

The probe is advisory and must never block streaming. Everything short of a definitive Miris
processing failure or a missing binding is reported as `unknown`, and the response still carries
the UUID so the client can attempt the stream.

Authorization is two-tier:
  - Tier 1: enforceAPI on the route
  - Tier 2: enforce GET on the asset named by databaseId and assetId, before any metadata is read
"""
import os
import re
from typing import Optional, Tuple

import boto3
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config

from common.addonApiRoutes import API_ADDON_MIRIS_ASSET_STATUS
from common.resourceNames import ResourceKeys, get_table_name
from customLogging.logger import safeLogger
from handlers.auth import request_to_claims
from handlers.authz import CasbinEnforcer
from models.common import (
    APIGatewayProxyResponseV2,
    VAMSGeneralErrorResponse,
    authorization_error,
    general_error,
    internal_error,
    success,
    validation_error,
    validation_error_message,
)
from models.addon.miris import (
    MIRIS_DISPLAY_NAME_KEY,
    MIRIS_SOURCE_VERSION_ID_KEY,
    MIRIS_UPLOADED_AT_KEY,
    STATE_NOT_BOUND,
    STATE_PROCESSING,
    STATE_UNKNOWN,
    VIEWER_KEY_MODE_PER_VIEW,
    VIEWER_KEY_MODE_STATIC,
    GetMirisAssetStatusRequestModel,
    MirisAssetStatusResponseModel,
)

from . import mirisIntegration
from .streamMetadata import (
    file_path_for_asset,
    load_readable_asset,
    read_stream_metadata,
    stream_uuid,
    unusable_uuid_state,
)

retry_config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
dynamodb = boto3.resource("dynamodb", config=retry_config)
logger = safeLogger(service_name="MirisGetAssetStatus")

claims_and_roles = {}

try:
    asset_storage_table_name = get_table_name(ResourceKeys.ASSET_STORAGE_TABLE)
    asset_file_metadata_table_name = get_table_name(ResourceKeys.ASSET_FILE_METADATA_STORAGE_TABLE)
    # Deployment-wide Miris viewer key, the same credential the frontend streams with (static mode).
    miris_viewer_key = os.environ.get("MIRIS_VIEWER_KEY", "").strip()
    miris_viewer_key_mode = os.environ.get("MIRIS_VIEWER_KEY_MODE", VIEWER_KEY_MODE_STATIC).strip()
except Exception:
    logger.exception("Failed loading resource names")
    raise

asset_table = dynamodb.Table(asset_storage_table_name)
asset_file_metadata_table = dynamodb.Table(asset_file_metadata_table_name)

# A Miris `state` is passed through only when it looks like a state token.
_MIRIS_STATE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def lambda_handler(event, context: LambdaContext) -> APIGatewayProxyResponseV2:
    global claims_and_roles
    claims_and_roles = request_to_claims(event)

    try:
        path = event["requestContext"]["http"]["path"]
        method = event["requestContext"]["http"]["method"]

        if len(claims_and_roles["tokens"]) > 0:
            casbin_enforcer = CasbinEnforcer(claims_and_roles)
            if not casbin_enforcer.enforceAPI(event):
                return authorization_error()
        else:
            return authorization_error()

        if method == "GET" and API_ADDON_MIRIS_ASSET_STATUS.matches(path):
            return _handle_get(event, context)
        return validation_error(body={"message": "Method not allowed"}, event=event)

    except ValidationError as v:
        logger.exception(f"Validation error: {v}")
        return validation_error(body={"message": validation_error_message(v)}, event=event)
    except VAMSGeneralErrorResponse as v:
        logger.exception(f"VAMS error: {v}")
        return general_error(body={"message": str(v)}, event=event)
    except Exception as e:
        logger.exception(f"Internal error: {e}")
        return internal_error(event=event)


def _handle_get(event, context):
    query_params = event.get("queryStringParameters") or {}

    request_model = parse(
        {
            "databaseId": query_params.get("databaseId"),
            "assetId": query_params.get("assetId"),
            "filePath": query_params.get("filePath"),
        },
        model=GetMirisAssetStatusRequestModel,
    )
    database_id = request_model.databaseId
    asset_id = request_model.assetId

    asset, denied = load_readable_asset(
        asset_table, CasbinEnforcer(claims_and_roles), database_id, asset_id, event
    )
    if denied:
        return denied

    # A full object key is reduced against this asset's own root key only.
    file_path = file_path_for_asset(asset, asset_id, request_model.filePath)

    stream_metadata = read_stream_metadata(
        asset_file_metadata_table, database_id, asset_id, file_path
    )

    miris_asset_uuid = stream_uuid(stream_metadata)
    response = MirisAssetStatusResponseModel(
        filePath=file_path,
        mirisAssetUuid=miris_asset_uuid or None,
        sourceVersionId=stream_metadata.get(MIRIS_SOURCE_VERSION_ID_KEY),
        uploadedAt=stream_metadata.get(MIRIS_UPLOADED_AT_KEY),
        displayName=stream_metadata.get(MIRIS_DISPLAY_NAME_KEY),
        state=_stream_state(
            miris_asset_uuid, database_id, asset_id, mirisIntegration.request_deadline(context, event)
        ),
    )
    return success(body=response.dict())


def _stream_state(miris_asset_uuid: str, database_id: str, asset_id: str, deadline: float) -> str:
    """Resolve the response `state` for the UUID stored on the file."""
    unusable_state = unusable_uuid_state(miris_asset_uuid)
    if unusable_state is not None:
        if miris_asset_uuid:
            logger.warning("miris_asset_uuid on the file is not a UUID")
        return unusable_state

    if miris_viewer_key_mode == VIEWER_KEY_MODE_PER_VIEW:
        miris_asset, state = _read_bound_asset(miris_asset_uuid, database_id, asset_id, deadline)
        return state if miris_asset is None else _usable_state(miris_asset.state)

    if miris_viewer_key_mode != VIEWER_KEY_MODE_STATIC or not miris_viewer_key:
        logger.warning("The Miris viewer key mode or the static viewer key is not configured")
        return STATE_UNKNOWN
    miris_asset, state = _read_bound_asset(miris_asset_uuid, database_id, asset_id, deadline)
    if miris_asset is None:
        return state
    return _viewer_processing_state(miris_asset_uuid, deadline)


def _usable_state(state: str) -> str:
    if not _MIRIS_STATE_PATTERN.fullmatch(state):
        logger.warning("Miris asset response contained no usable state")
        return STATE_UNKNOWN
    return state


def _read_bound_asset(miris_asset_uuid: str, database_id: str, asset_id: str,
                      deadline: float) -> Tuple[Optional[mirisIntegration.MirisAsset], str]:
    """Read the Miris asset with the Integration Key and check its binding to this VAMS asset.

    Returns `(asset, "")` for a bound asset, or `(None, state)` with `notBound` for a missing or
    unbound asset and `unknown` when the read failed.
    """
    try:
        miris_asset = mirisIntegration.get_asset(miris_asset_uuid, deadline=deadline)
    except mirisIntegration.MirisIntegrationError as e:
        logger.warning(f"Miris asset read failed for {miris_asset_uuid}: {e}")
        return None, STATE_UNKNOWN

    if miris_asset is None:
        logger.warning(f"Miris has no asset {miris_asset_uuid}")
        return None, STATE_NOT_BOUND
    if not mirisIntegration.is_bound_to_asset(miris_asset.tags, database_id, asset_id):
        logger.warning(f"Miris asset {miris_asset_uuid} is not tagged for {database_id}/{asset_id}")
        return None, STATE_NOT_BOUND
    return miris_asset, ""


def _viewer_processing_state(miris_asset_uuid: str, deadline: float) -> str:
    """Ask the Miris viewer API, with the deployment-wide viewer key, for the processing state."""
    try:
        state = mirisIntegration.get_viewer_asset_state(miris_asset_uuid, miris_viewer_key, deadline=deadline)
    except mirisIntegration.MirisIntegrationError as e:
        if e.status_code == 400:
            # Miris answers 400 until a freshly uploaded asset has finished processing.
            logger.info(f"Miris viewer API returned 400 for {miris_asset_uuid}, asset is processing")
            return STATE_PROCESSING
        logger.warning(f"Miris viewer asset read failed for {miris_asset_uuid}: {e}")
        return STATE_UNKNOWN

    if state is None:
        # Not visible to the viewer key. This is not proof the asset is gone: it also covers
        # an asset still being ingested and any workspace-visibility drift.
        logger.warning(f"Miris viewer API returned 404 for {miris_asset_uuid}")
        return STATE_UNKNOWN
    return _usable_state(state)
