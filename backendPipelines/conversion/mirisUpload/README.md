# Miris Spatial Streaming Upload Pipeline

The Miris Spatial Streaming Upload pipeline uploads USD source assets to [Miris Spatial Streaming](https://miris.com) and records the resulting Miris asset as file metadata on the asset's root USD file, so the file becomes streamable in the VAMS Miris viewer. It auto-registers as a standard VAMS pipeline and GLOBAL workflow (`miris-upload`) with a `fileUpload` trigger, so it runs automatically when a matching USD file is uploaded, or on demand from the file manager's **Automation -> Execute Workflow** action against one of the asset's USD files.

The pipeline is part of the Miris Spatial Streaming add-on (`app.addons.useMirisStreaming`), which provides the viewer plugin that streams the uploaded asset and holds the Miris Integration Key and API base URL the pipeline uses. The add-on deploys it when `app.addons.useMirisStreaming.upload.enabled` is `true`, as a nested stack of the add-on stack, and then publishes the `MIRIS_UPLOAD` feature switch that offers the viewer's **Stream with Miris** button.

The pipeline is same-Region only: `vamsExecuteMirisUpload` rejects an input file or output bucket in another AWS Region than the deployment before anything is claimed or uploaded, and reports the cause on the workflow task.

## Pipeline Components

### Container (`container/`)

-   **`__main__.py`** - Entrypoint; lists the asset under its resolved root, chooses the root USD file, checks the size limit, runs the Miris upload flow and writes the stream metadata for the root file
-   **`stream_metadata.py`** - Builds the stream metadata body and its `.metadata.json` key under the run's metadata output path
-   **`usd_packager.py`** - Root selection (the layer no other layer references), USD dependency resolution and `.usdz` packaging via OpenUSD (`UsdUtils.ComputeAllDependencies`)
-   **`upload_config.py`** - Reads the rendered template configuration (Miris asset name and tags) and drops tags with the reserved `vams-asset-` and `vams-database-` prefixes
-   **`miris_uploader.py`** - Miris content API client (`MirisClient`): start upload, mark complete, poll, generate
-   **`utils/secrets.py`** - Retrieves the Miris Integration Key from AWS Secrets Manager
-   **`Dockerfile`** / **`entrypoint.sh`** - x86_64 image, digest-pinned base, runs as a non-root user

### Lambda Functions (`lambda/`)

-   **`mirisUploadGate.py`** - Collapses a per-file trigger fan-out to a single upload: a multi-file USD asset can fire the `fileUpload` trigger once per matching layer, and the gate claims a fingerprint of the asset's USD files with a conditional S3 put so only the first execution proceeds; manual runs skip the claim
-   **`mirisClaim.py`** - Claim-key derivation and release, shared by the gate and every path that releases a claim
-   **`vamsExecuteMirisUpload.py`** - VAMS API integration; rejects a cross-Region bucket, invokes the gate, then the pipeline
-   **`openPipeline.py`** - Starts the inner Step Functions execution; rejects folder inputs (requires a single file)
-   **`constructPipeline.py`** - Builds the AWS Batch job definition from the pipeline payload; fails the run before the job is submitted when `outputS3AssetMetadataPath` is missing
-   **`pipelineEnd.py`** - Reports the workflow callback (`SendTaskSuccess` / `SendTaskFailure`), releases the claim when the run did not succeed, and retires the asset's other claims when it did

### CDK Infrastructure (`../../../infra/lib/nestedStacks/addon/miris/upload/`)

-   **`mirisUploadBuilder-nestedStack.ts`** - Main CDK stack definition, built by `AddonBuilderNestedStack` beside the add-on's `MirisBuilderNestedStack`
-   **`constructs/mirisUpload-construct.ts`** - Core pipeline infrastructure (AWS Batch, Step Functions, workflow auto-registration)
-   **`lambdaBuilder/mirisUploadFunctions.ts`** - Lambda function definitions

## Pipeline Process

1. **Gate check**

    - A manual run (the **Miris Spatial Streaming Upload** workflow from **Automation -> Execute Workflow**) always proceeds and writes no claim.
    - A triggered run lists the asset's files with a trigger extension under the asset root and fingerprints their asset-relative keys and ETags (SHA-256). The gate claims `s3://<auxiliary-bucket>/locks/miris-upload/<databaseId>/<assetId>/<fingerprint>.claim` with a conditional S3 put (`If-None-Match: *`). The first execution to write it proceeds; every other execution for the same content (for example, several layers uploaded together, or a repeat trigger on unchanged files) receives `PreconditionFailed`, reports its workflow task token as skipped, and exits as a no-op.
    - A run that does not succeed releases its claim. A successful run keeps its claim and deletes the asset's claims on other content, so changed, added or reverted USD layers upload again. A change to non-USD files alone (textures) gives the same fingerprint; run the **Miris Spatial Streaming Upload** workflow to upload it.

2. **List, size check, root selection and download**

    - Lists the asset under its resolved root (which carries `baseAssetsPrefix` or an existing-key location) and checks `maxAssetSizeBytes` from the listing before downloading: a lone `.usdz` root counts alone, otherwise every file under the root counts.
    - Chooses the root: the one file with a trigger extension that no other USD layer references (sublayers, references, payloads, value clips, asset-valued attributes), or the single `.usdz`. More than one candidate fails with `ambiguous_root` (exit 10); an unreadable layer fails with `unreadable_layer` (exit 11) and the log names the file. A dependency that resolves outside the asset directory (an absolute or `..` path) fails with `reference_outside_asset` (exit 12) before anything is packaged.
    - Reads the selected file at the S3 version the run recorded at launch (`triggerInput.versionId`) and the other layers at their current version; a launch version that no longer exists fails with `input_version_missing` (exit 13), and the log names the file and the version.
    - Downloads the asset preserving its relative layout so USD references resolve, computes the root's dependencies, and fails fast on unresolved (absolute-path) references.

3. **Package**

    - A multi-file USD root is packaged into a single self-contained `.usdz` (the Miris content API accepts one file per asset). A `.usdz`, or a dependency-free USD, is uploaded as-is.

4. **Upload to Miris**

    - `POST /v1/content` to start the upload and obtain a Miris asset UUID + short-lived STS credentials.
    - SigV4-signed S3 `PUT` to the Miris temp endpoint (object key matches the declared `content_path` verbatim).
    - `PUT /v1/content/{id}` to mark the upload complete.
    - Before `POST /v1/content`, the container checks the time left before the run's deadline: the earlier of the inner state machine's timeout (counted from the execution start, so the job queue wait counts against it) and the Batch attempt's, less 300 seconds for the end of the run. When that does not cover the upload estimate (120 seconds plus 5 MiB per second of artifact), it exits 14 (`insufficient_time`) and nothing reaches Miris.

5. **Emit and poll**

    - Writes `{outputS3AssetMetadataPath}<relativeFilePath>.metadata.json` as soon as the upload is marked complete, where `<relativeFilePath>` is the root USD file's path within the asset root (for example `scenes/pump.usda`). The workflow's process-output step applies it to that file as an `update` when the run succeeds, so other file metadata is kept.
    - Polls until Miris reaches a terminal preview/streamable state, for at most `taskTimeoutSeconds` or until the run's deadline, then requests streamable processing (`POST /v1/asset/{id}/generate`, best effort). When the wait runs out first, the run succeeds with the file linked to the still-processing asset and skips the request (`processing_unfinished`); an asset left at `preview` is promoted in the Miris Portal.
    - A failure after `POST /v1/content` (a Miris `error` or `failed` state, a failed upload `PUT` or metadata write) fails the run but never deletes the Miris asset; the container logs its ID (`miris_asset_kept`). The workflow applies no stream metadata for a failed run, so the asset is not linked to the VAMS file: find it in the Miris Portal by its `vams-asset-{assetId}` tag, then link it by setting the file's stream metadata by hand, or delete it there. Running the workflow again uploads a new Miris asset.

## Output

The pipeline writes no asset files. It sets four file metadata keys on the asset's root USD file:

| Key                       | Type     | Value                                                                                     |
| ------------------------- | -------- | ----------------------------------------------------------------------------------------- |
| `miris_asset_uuid`        | `string` | The Miris asset UUID to stream                                                            |
| `miris_source_version_id` | `string` | S3 `VersionId` of the USD object the container downloaded; empty when the bucket has none |
| `miris_uploaded_at`       | `date`   | ISO 8601 UTC time the upload completed, for example `2026-10-14T09:30:00Z`                |
| `miris_display_name`      | `string` | Display name used for the Miris asset                                                     |

A later upload (a manual run, or a trigger on changed USD content) overwrites the four keys with the new Miris asset; the previous Miris asset is not deleted.

Every Miris asset the pipeline creates carries the tags `vams`, `vams-asset-{assetId}` and `vams-database-{databaseId}`. Template tags (`MIRIS_TAGS`) that begin with `vams-asset-` or `vams-database-` are dropped with a warning.

## Configuration Parameters

Configured under `app.addons.useMirisStreaming.upload.*` in `infra/config/config.json`:

-   `enabled` - Deploys the pipeline
-   `triggerExtensions` - Auto-trigger extensions (default `.usd,.usda,.usdc,.usdz`); also the files the gate fingerprints and the container considers as root
-   `taskTimeoutSeconds` - Maximum time the container waits for Miris to report `preview` or `streamable`; the wait also ends at the run's deadline
-   `maxAssetSizeBytes` - Maximum source size, checked from the listing before download
-   `autoRegisterWithVAMS` / `autoRegisterAutoTriggerOnFileUpload` - Workflow auto-registration and upload auto-trigger

The pipeline also reads two of the add-on's own settings, under `app.addons.useMirisStreaming.*`:

-   `apiKeySecretArn` - AWS Secrets Manager ARN holding the Miris Integration Key
-   `mirisApiBaseUrl` - Miris API base URL (default `https://app.miris.com`)

## AWS Resources

-   **AWS Batch** (Fargate) - Runs the upload container
-   **Step Functions** - Pipeline orchestration
-   **Lambda** - Gate, execute, construct, open, and end handlers
-   **Amazon S3** - Asset input and stream metadata output storage
-   **AWS Secrets Manager** - Miris Integration Key
-   **Amazon ECR** - Container image storage

## Usage

1. Upload a USD asset (`.usd`, `.usda`, `.usdc`, `.usdz`) to a VAMS asset; the auto-registered trigger fires and the pipeline starts. A multi-file asset can fire the trigger once per layer; the gate collapses that fan-out so the content is uploaded once. To launch the pipeline on demand instead, select one of the asset's USD files in the file manager and choose **Automation -> Execute Workflow**, then pick the **Miris Spatial Streaming Upload** workflow and the **Stream with Miris** template. The workflow takes a single file, so a whole-asset (`/`) selection is rejected in the execute wizard.
2. The pipeline uploads the asset's root to Miris and waits until Miris reports it as `preview` or `streamable`, then requests streamable processing (typically 1–2 hours).
3. When the run completes, the `miris_*` keys appear in the root USD file's metadata.
4. Open the root USD file in the Miris viewer to stream the asset.

## Requirements

-   `app.addons.useMirisStreaming.enabled` and `app.addons.useMirisStreaming.upload.enabled` set to `true`
-   Miris Integration Key stored in AWS Secrets Manager, referenced by `app.addons.useMirisStreaming.apiKeySecretArn`
-   `app.useGlobalVpc.enabled` set to `true`; the Batch jobs reach Miris through the NAT gateway of the private subnets
-   The commercial `aws` partition only
-   Asset buckets in the deployment Region; an input or output bucket in another Region fails the run
