# Add-on API Reference

This page documents every VAMS REST API surface contributed by an optional [add-on](../developer/addons-overview.md). Each add-on's endpoints live under `/addon/{addonName}/...` and are only deployed when the corresponding add-on is enabled in `infra/config/config.json`.

Add-on endpoints enforce the same VAMS two-tier Casbin authorization as the core API: API-tier (can the role reach the route) and object-tier (can the role access the specific data entity). Both tiers must allow for a call to succeed.

---

## Physna add-on

The Physna add-on surfaces endpoints that gate Physna-hosted functionality behind VAMS authorization. All routes below are only deployed when `app.addons.usePhysnaSync.enabled` is `true`.

See the [Physna Integration](../developer/physna-integration.md) developer guide for broader context on the add-on, its sync behavior, and its configuration.

### GET `/addon/physna/viewer`

Returns a JSON envelope describing whether the Physna viewer can currently be rendered for a given asset file, and — when it can — the values the frontend needs to embed Physna's hosted viewer in an `<iframe>` directly.

:::warning[The viewer token is not scoped to an asset]
The `viewerToken` field in the `ready` response carries no asset scope. `POST /viewer/token` accepts no asset parameter, so Physna issues the token at tenant scope, and every synced database and asset lives under the single tenant named in `tenantId`. Physna sets the token's lifetime; VAMS reads no expiry and mints a fresh token on each `ready` response.

The token is returned to the browser so the iframe can load Physna's hosted viewer directly, which avoids the Lambda response-size limits and URL-rewriting complexity of proxying the full viewer payload. VAMS's object-tier authorization runs on every call to this endpoint — a user who cannot see the asset cannot obtain a viewer token for it, but the token they receive is not restricted to it. See [Physna Viewer](../developer/physna-integration.md#physna-viewer) for the limitation and its side effects.
:::

#### Query parameters

| Parameter      | Type   | Required | Description                                                     |
| -------------- | ------ | -------- | --------------------------------------------------------------- |
| `databaseId`   | string | Yes      | VAMS database ID the asset belongs to.                          |
| `assetId`      | string | Yes      | VAMS asset ID.                                                  |
| `relativePath` | string | Yes      | Relative path of the file within the asset, beginning with `/`. |

#### Responses

All responses have `Content-Type: application/json`. The body always includes `status` and `message`; additional fields are populated for specific statuses.

| `status`               | HTTP  | Additional fields                                           | Meaning                                                                   |
| ---------------------- | ----- | ----------------------------------------------------------- | ------------------------------------------------------------------------- |
| `ready`                | `200` | `physnaAssetId`, `tenantId`, `viewerToken`, `physnaApiBase` | Asset is ready to render. Use the fields to build the Physna iframe URL.  |
| `indexing`             | `200` | `physnaState`                                               | Physna is still indexing the asset. Poll again shortly.                   |
| `not_synced`           | `200` | —                                                           | File has not appeared in Physna yet. Poll again shortly.                  |
| `failed`               | `200` | `physnaState`                                               | Physna reported a permanent failure state; the viewer cannot render.      |
| `unsupported`          | `400` | —                                                           | File extension is not in the Physna-supported list.                       |
| `not_found`            | `404` | —                                                           | Asset does not exist in VAMS.                                             |
| `forbidden`            | `403` | —                                                           | Caller lacks API- or object-level access.                                 |
| `invalid_request`      | `400` | —                                                           | Query parameters failed validation.                                       |
| `request_failed`       | `400` | —                                                           | The request could not be completed against Physna.                        |
| `method_not_allowed`   | `405` | —                                                           | Non-`GET` request.                                                        |
| `upstream_unavailable` | `502` | —                                                           | Physna API could not be reached, or the viewer token could not be minted. |
| `internal_error`       | `500` | —                                                           | Unexpected server error.                                                  |

#### Example request

```
GET /addon/physna/viewer?databaseId=engineering&assetId=pump-housing-v2&relativePath=%2Fpump.stp
```

#### Example `ready` response

```json
{
    "status": "ready",
    "message": "Physna viewer is ready.",
    "tenantId": "bc336bab-e3e4-4c50-bd00-9d6cbbd9f194",
    "physnaAssetId": "8d65af7c-5412-404b-a494-fbd2cdb62442",
    "viewerToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "physnaApiBase": "https://app-api.physna.com/v3"
}
```

The frontend uses these fields to build the iframe URL:

```
${physnaApiBase}/tenants/${tenantId}/viewer/asset
  ?assetId=${physnaAssetId}
  &token=${viewerToken}
  &theme=dark
  &parentOrigin=${window.location.origin}
```

#### Example `indexing` response

```json
{
    "status": "indexing",
    "message": "Physna is still indexing this file. Please check back shortly.",
    "physnaState": "indexing"
}
```

#### Authorization

-   **API tier:** The caller's role must allow `GET` on `/addon/physna/viewer` through a permission constraint with `objectType: "api"`; a `web` constraint does not grant it. The seeded `admin` and `basicReadOnly` roles and the Database Admin, Database User, Database Read-Only, and Global Read-Only permission templates grant the route.
-   **Object tier:** The caller must have `GET` permission on the specific `databaseId` / `assetId` combination (`objectType: "asset"`).

Both tiers must allow for the endpoint to succeed. Failures return `status: "forbidden"` with HTTP `403`.

---

## Miris Spatial Streaming add-on

The Miris Spatial Streaming add-on serves the two routes the Miris viewer plugin calls for a USD file: the stream status, and a viewer key that can stream the file's Miris asset. Both routes are deployed only when `app.addons.useMirisStreaming.enabled` is `true`. They serve the browser viewer; the VAMS CLI and MCP server do not call them.

See [Miris Spatial Streaming](../developer/miris-spatial-streaming.md) for the add-on's viewer, viewer key modes, and configuration.

Both routes read the Miris asset UUID server side from the file's `miris_asset_uuid` metadata. The request names only the database, asset, and file, so a caller can reach only a stream that the file itself references. Before any Miris call that depends on the UUID, both routes read the Miris asset with the Miris Integration Key and check its binding tags, `vams-asset-{assetId}` and `vams-database-{databaseId}`. A Miris asset that lacks either tag, or that Miris reports as missing, is `notBound`.

### Request fields

Both routes take the same three fields: as query parameters on the status route, and as a JSON body on the viewer key route.

| Field        | Type   | Required | Description                                                                                                                                                                                                           |
| ------------ | ------ | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `databaseId` | string | Yes      | VAMS database ID the asset belongs to.                                                                                                                                                                                |
| `assetId`    | string | Yes      | VAMS asset ID.                                                                                                                                                                                                        |
| `filePath`   | string | Yes      | The file's asset-relative path (`/folder/scene.usd`) or its full object key. A key under the asset's root key, or under `{assetId}/`, is reduced to the asset-relative path. A folder path ending in `/` is rejected. |

### GET `/addon/miris/asset-status`

Returns the stream metadata recorded on the file and the stream's state. The state is advisory: anything short of a definitive Miris processing failure or a missing binding is reported as `unknown`, and the response still carries the UUID so the viewer can attempt the stream.

#### Response fields (200)

| Field             | Type           | Description                                                                                                 |
| ----------------- | -------------- | ----------------------------------------------------------------------------------------------------------- |
| `filePath`        | string         | The normalized asset-relative path, for example `/scene.usd`.                                               |
| `mirisAssetUuid`  | string or null | `miris_asset_uuid`, or null when it is absent or empty.                                                     |
| `sourceVersionId` | string or null | `miris_source_version_id`, or null when absent. Empty when the source version was not known at upload time. |
| `uploadedAt`      | string or null | `miris_uploaded_at` (ISO 8601), or null when absent.                                                        |
| `displayName`     | string or null | `miris_display_name`, or null when absent.                                                                  |
| `state`           | string         | One of the states below.                                                                                    |

#### States

| `state`       | Meaning                                                                                                                                                                                                                                                                                             |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `notStreamed` | `miris_asset_uuid` is absent or empty. Miris is not called.                                                                                                                                                                                                                                         |
| `invalidUuid` | `miris_asset_uuid` is set but is not a UUID. Miris is not called.                                                                                                                                                                                                                                   |
| `notBound`    | Miris has no such asset (`404`), or the asset lacks either binding tag.                                                                                                                                                                                                                             |
| `unknown`     | The processing state could not be established: the add-on is not fully configured for its viewer key mode (Miris is not called), or Miris was unreachable, did not answer before the request deadline, returned a non-2xx status (including the viewer API's `404`), or returned no usable `state`. |
| `processing`  | The Miris viewer API answered `400`, which it does while Miris prepares the asset. Reported in `static` mode only.                                                                                                                                                                                  |
| Miris state   | The processing state Miris reports, passed through: `preview` while Miris prepares the asset, `streamable` once it can be streamed, and `error` or `failed` for a definitive processing failure.                                                                                                    |

After the binding check, `static` mode reads the processing state from the Miris viewer API with the deployment viewer key, the same credential the viewer streams with. `perView` mode, which has no deployment-wide viewer key, reads it from the Integration API response used for the binding check.

#### Errors

| HTTP  | Cause                                                  |
| ----- | ------------------------------------------------------ |
| `400` | Invalid query parameters, or the asset does not exist. |
| `403` | Tier 1 or Tier 2 authorization denied.                 |
| `500` | Unexpected server error.                               |

#### Example request

```
GET /addon/miris/asset-status?databaseId=engineering&assetId=pump-housing&filePath=%2Fscenes%2Fpump.usda
```

#### Example response

```json
{
    "filePath": "/scenes/pump.usda",
    "mirisAssetUuid": "3f2b8c1e-6a4d-4e1f-9b7a-2c5d8e9f0a1b",
    "sourceVersionId": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
    "uploadedAt": "2026-10-14T09:30:00Z",
    "displayName": "Pump housing",
    "state": "streamable"
}
```

### POST `/addon/miris/viewer-key`

Returns a viewer key that can stream the Miris asset recorded on the file, after both authorization tiers and the binding check pass. What is returned follows the deployment's `viewerKeyMode`:

-   **`static`**: the deployment-wide viewer key. No key is created and nothing is stored; `expiresAt` is `null`. Each delivery writes a `mirisStaticViewerKeyIssue` audit record.
-   **`perView`**: a key created with the Integration Key for the one Miris asset, with no tags, that expires `viewerKeyTtlMinutes` minutes after it is minted. Its key ID is recorded in the add-on's viewer key table, and a cleanup function deletes the key from Miris after it expires. Each mint writes a `mirisViewerKeyMint` audit record.

The key material is returned to the caller only, and is never stored, logged, or audited. Responses carry `Cache-Control: no-cache, no-store`.

:::warning[The static key is deployment-wide]
In `static` mode, any caller who passes the checks for one bound asset receives the deployment's viewer key, which streams every Miris asset that key allows. Scope the key in Miris with tags and an expiry, or use `perView` mode. See [Static mode](../developer/miris-spatial-streaming.md#static-mode).
:::

#### Request body

```json
{
    "databaseId": "engineering",
    "assetId": "pump-housing",
    "filePath": "/scenes/pump.usda"
}
```

#### Response fields (200)

| Field            | Type           | Description                                                                 |
| ---------------- | -------------- | --------------------------------------------------------------------------- |
| `filePath`       | string         | The normalized asset-relative path, for example `/scene.usd`.               |
| `mirisAssetUuid` | string         | The Miris asset the key can stream.                                         |
| `viewerKey`      | string         | The viewer key material.                                                    |
| `expiresAt`      | string or null | The key's expiry, RFC 3339 UTC, in `perView` mode; `null` in `static` mode. |

#### Example `perView` response

```json
{
    "filePath": "/scenes/pump.usda",
    "mirisAssetUuid": "3f2b8c1e-6a4d-4e1f-9b7a-2c5d8e9f0a1b",
    "viewerKey": "<viewer key material>",
    "expiresAt": "2026-10-14T09:45:00Z"
}
```

#### Errors

Errors carry `message`, and `state` when the error concerns the file's stream.

| HTTP  | `state`       | Cause                                                                                                                                        |
| ----- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `400` | `invalidUuid` | `miris_asset_uuid` is not a UUID.                                                                                                            |
| `400` | --            | Invalid request body, or the asset does not exist.                                                                                           |
| `403` | `notBound`    | The Miris asset is missing, or lacks the asset's binding tags. The message names both tags.                                                  |
| `403` | --            | Tier 1 or Tier 2 authorization denied.                                                                                                       |
| `404` | `notStreamed` | The file records no Miris stream.                                                                                                            |
| `500` | --            | A per-view key was minted but could not be recorded (VAMS attempts to delete the key from Miris), or the configured key lifetime is invalid. |
| `502` | --            | Miris could not be reached, or did not create the key.                                                                                       |
| `503` | --            | The viewer key mode, the Integration Key secret, the Miris API base URL, or, in `static` mode, the deployment viewer key is not configured.  |
| `504` | --            | The request deadline passed before Miris answered.                                                                                           |

### Authorization

-   **API tier:** the caller's role must allow `GET` on `/addon/miris/asset-status` and `POST` on `/addon/miris/viewer-key` through constraints with `objectType: "api"`. The Database Admin, Database User, Database Read-Only, and Global Read-Only permission templates grant both routes, and the seeded `basicReadOnly` role gains both when the add-on is enabled.
-   **Object tier:** the caller must have `GET` permission on the `databaseId` / `assetId` combination (`objectType: "asset"`). The viewer key route checks `GET`, not `POST`, because a viewer key grants streaming only.

Both tiers must allow for a route to succeed. The asset is authorized before any of its metadata is read.
