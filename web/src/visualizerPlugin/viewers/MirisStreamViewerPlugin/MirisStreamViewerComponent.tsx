/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { ViewerPluginProps } from "../../core/types";
import Synonyms from "../../../synonyms";
import { fetchFileInfo } from "../../../services/APIService";
import {
    ASSET_EXECUTIONS_API_ROUTE,
    EXECUTE_WORKFLOW_API_ROUTE,
    findRunningMirisUpload,
    getMirisStreamStatus,
    MIRIS_REQUEST_BLOCKED,
    mintMirisViewerKey,
    startMirisUpload,
} from "../../../services/addons/mirisAPIService";
import { useAllowedRoutes } from "../../../features/orchestration/permissions/useAllowedRoutes";
import { appCache } from "../../../services/appCache";
import {
    EXECUTION_DETAILS_API_ROUTE,
    executionDetailPath,
} from "../../../components/filemanager/utils/executionLinks";
import {
    acceptsMirisUpload,
    isKnownVersionId,
    isStaleStream,
    latestVersionIdFromFileInfo,
    streamDisposition,
    toStatusFilePath,
} from "./streamStatus";
import { MirisStreamDependencyManager } from "./dependencies";
import type { MirisSceneElement, MirisStreamElement } from "./mirisElements";
import styles from "./MirisStreamViewer.module.css";

// Add-on feature switch (ADDON_APP_FEATURES.MIRIS_UPLOAD) published when the deployment includes the
// Miris upload pipeline that "Stream with Miris" runs.
const MIRIS_UPLOAD_FEATURE = "MIRIS_UPLOAD";

/** True when the deployment's secure-config lists the Miris upload pipeline's feature switch. */
function uploadPipelineDeployed(): boolean {
    return !!appCache.getItem("config")?.featuresEnabled?.includes(MIRIS_UPLOAD_FEATURE);
}

type ViewerError =
    | { kind: "NOT_CONFIGURED" }
    | { kind: "STATUS_FETCH" }
    | { kind: "FORBIDDEN" }
    | { kind: "MINT_FAILED" }
    | { kind: "REQUEST_BLOCKED"; message: string }
    | { kind: "INVALID_UUID" }
    | { kind: "NOT_BOUND"; databaseId: string; assetId: string }
    | { kind: "LOAD_FAILED" }
    | { kind: "STREAM_TIMEOUT"; uuid: string }
    | { kind: "DEVICE_UNSUPPORTED" }
    | { kind: "MIRIS_PROCESSING_ERROR" };

type ProcessingState = { state: string };

// The file records no Miris stream; `filePath` is its asset-relative path, which an upload names.
type NotStreamed = { filePath: string };

// "Stream with Miris" request from the not-yet-streamed state.
type UploadRequest = { kind: "starting" } | { kind: "failed"; message: string };

// An upload has started and the file does not record its stream yet.
type UploadStarted = { executionId: string | null };

// Lookup of an unfinished Miris upload run on the asset, made before the button is offered.
type RunningUpload = { kind: "checking" } | { kind: "running"; executionId: string };

type StreamInfo = {
    uuid: string;
    displayName: string | null;
    sourceVersionId: string | null;
};

// A scene is rendered only once its viewer key exists: the SDK does not retry a stream that
// started without one, so a new key mounts a new scene (`generation` is its React key).
type SceneConfig = {
    uuid: string;
    viewerKey: string;
    generation: number;
};

// Poll Miris status every 90s while an asset is still being prepared on the
// Miris side. Typical promotion to streamable takes 1–2 hours.
const PROCESSING_POLL_INTERVAL_MS = 90_000;

// First-frame load includes the WebSocket handshake, viewer key validation and the initial
// fetch from the content CDN; 60s gives cold connections headroom while still failing fast on
// a genuinely unreachable asset. Auth and CSP failures fire no event, so this is their signal.
const STREAM_LOAD_TIMEOUT_MS = 60_000;

// The SDK load awaits a WebGPU adapter request and three chunk fetches, any of which can stall
// on a broken GPU process or a stalled connection without ever rejecting; 60s covers a cold
// fetch of the SDK bundle on a slow link.
const SDK_LOAD_TIMEOUT_MS = 60_000;

function errorMessage(err: ViewerError): string {
    switch (err.kind) {
        case "NOT_CONFIGURED":
            return "Miris streaming is not configured for this deployment. Contact your administrator.";
        case "STATUS_FETCH":
            return "Could not load the Miris stream status for this file. Try again.";
        case "FORBIDDEN":
            return "You do not have permission to stream this asset.";
        case "MINT_FAILED":
            return "Could not get access to stream this asset from Miris. Try again.";
        case "REQUEST_BLOCKED":
            return err.message;
        case "INVALID_UUID":
            return "This file's miris_asset_uuid metadata is not a valid UUID. Correct it in the file's metadata, or run the Miris Spatial Streaming Upload workflow on it again.";
        case "NOT_BOUND":
            return (
                "The Miris asset recorded for this file is not linked to this VAMS asset: it must " +
                `exist in the Miris workspace and carry the vams-asset-${err.assetId} and ` +
                `vams-database-${err.databaseId} tags. Run the Miris Spatial Streaming Upload workflow on this file again, ` +
                "or add those tags to the Miris asset."
            );
        case "LOAD_FAILED":
            return "The Miris viewer failed to load. Try again.";
        case "STREAM_TIMEOUT":
            return (
                `Timed out waiting for Miris to start streaming asset ${err.uuid}. ` +
                "The asset reached a streamable state but the first frame didn't arrive in time. " +
                "Try again; if it keeps timing out, verify the asset still exists in app.miris.com."
            );
        case "DEVICE_UNSUPPORTED":
            return (
                "Miris streaming is not supported on this device: the browser's WebGL 2.0 support " +
                "is unavailable or incomplete. Supported browsers: Chrome 90+, Firefox 88+, " +
                "Safari 14+, Edge 90+. Some Miris assets (GES) need WebGPU, so a WebGPU-capable " +
                "browser such as a current Chrome or Edge is recommended."
            );
        case "MIRIS_PROCESSING_ERROR":
            return "Miris processing failed. Check this asset in app.miris.com.";
    }
}

/**
 * Viewer for USD source files (.usd/.usda/.usdc/.usdz) that streams the Miris asset recorded
 * in the file's `miris_*` metadata. The stream status endpoint resolves that metadata server
 * side, so the viewer sends only the file path:
 *   - notStreamed  -> "Not yet streamed" guidance, with a "Stream with Miris" button that starts
 *                     the upload workflow when the deployment includes the Miris upload pipeline
 *                     (MIRIS_UPLOAD) and the user is allowed to run workflows; once started, the
 *                     status is polled until the file records its stream. While an upload run on
 *                     the asset has not finished, an in-progress notice replaces the button
 *   - invalidUuid  -> error
 *   - notBound     -> error: the Miris asset is not tagged for this VAMS asset
 *   - Miris still preparing the asset -> processing overlay, polled until it is streamable
 *   - otherwise    -> stream the returned UUID, with a stale banner when the stream was
 *                     built from a different version of the file than the one viewed
 */
const MirisStreamViewerComponent: React.FC<ViewerPluginProps> = ({
    assetId,
    databaseId,
    assetKey,
    versionId,
    assetVersionId,
}) => {
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<ViewerError | null>(null);
    const [notStreamed, setNotStreamed] = useState<NotStreamed | null>(null);
    const [uploadRequest, setUploadRequest] = useState<UploadRequest | null>(null);
    const [uploadStarted, setUploadStarted] = useState<UploadStarted | null>(null);
    const [runningUpload, setRunningUpload] = useState<RunningUpload | null>(null);
    const [streamInfo, setStreamInfo] = useState<StreamInfo | null>(null);
    const [staleStream, setStaleStream] = useState(false);
    const [processingState, setProcessingState] = useState<ProcessingState | null>(null);
    // Viewer key and asset of the rendered scene, held in memory for this open only.
    const [scene, setScene] = useState<SceneConfig | null>(null);
    // Bumped to re-run load(): by the status poll when the state changes, and by Retry.
    const [reloadCount, setReloadCount] = useState(0);

    const sceneRef = useRef<MirisSceneElement>(null);
    const streamRef = useRef<MirisStreamElement>(null);
    const sceneGenerationRef = useRef(0);
    const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
    // Bumped by every load, so an upload request that resolves after the viewed file changed is dropped.
    const loadGenerationRef = useRef(0);

    const { can } = useAllowedRoutes();
    const canExecuteWorkflow = can("POST", EXECUTE_WORKFLOW_API_ROUTE);
    const canViewExecution = can("GET", EXECUTION_DETAILS_API_ROUTE);
    const canListExecutions = can("GET", ASSET_EXECUTIONS_API_ROUTE);

    useEffect(() => {
        let cancelled = false;
        let sdkLoadTimer: ReturnType<typeof setTimeout> | undefined;
        loadGenerationRef.current += 1;

        async function load() {
            try {
                if (!assetKey) {
                    setStreamInfo(null);
                    setScene(null);
                    setError(null);
                    setProcessingState(null);
                    setNotStreamed(null);
                    setUploadRequest(null);
                    setUploadStarted(null);
                    setLoading(false);
                    return;
                }
                setLoading(true);
                setError(null);
                setNotStreamed(null);
                setUploadRequest(null);
                setUploadStarted(null);
                setScene(null);

                // 1. Stream status of the viewed file
                const filePath = toStatusFilePath(assetKey);
                const statusResult = await getMirisStreamStatus({ databaseId, assetId, filePath });
                if (cancelled) return;
                if (statusResult[0] !== true) {
                    const [, message, fetchStatus, fetchState] = statusResult;
                    if (fetchState === MIRIS_REQUEST_BLOCKED) {
                        setError({ kind: "REQUEST_BLOCKED", message });
                    } else if (fetchStatus === 403) {
                        setError({ kind: "FORBIDDEN" });
                    } else {
                        setError({ kind: "STATUS_FETCH" });
                    }
                    return;
                }
                const status = statusResult[1];

                const disposition = streamDisposition(status.state);
                if (disposition === "notStreamed") {
                    setStreamInfo(null);
                    setProcessingState(null);
                    setNotStreamed({ filePath: status.filePath });
                    setLoading(false);
                    return;
                }
                if (disposition === "invalidUuid") {
                    setError({ kind: "INVALID_UUID" });
                    return;
                }
                if (disposition === "notBound") {
                    setError({ kind: "NOT_BOUND", databaseId, assetId });
                    return;
                }
                if (disposition === "failed") {
                    setError({ kind: "MIRIS_PROCESSING_ERROR" });
                    return;
                }
                if (!status.mirisAssetUuid) {
                    setError({ kind: "STATUS_FETCH" });
                    return;
                }
                const uuid = status.mirisAssetUuid;
                setStreamInfo({
                    uuid,
                    displayName: status.displayName,
                    sourceVersionId: status.sourceVersionId,
                });

                // 2. Miris is still preparing the asset: overlay + poll. Any other
                // disposition re-runs load(), which handles it from the top.
                if (disposition === "processing") {
                    setProcessingState({ state: status.state });
                    setLoading(false);
                    if (pollIntervalRef.current) {
                        clearInterval(pollIntervalRef.current);
                    }
                    pollIntervalRef.current = setInterval(async () => {
                        try {
                            const r = await getMirisStreamStatus({
                                databaseId,
                                assetId,
                                filePath,
                            });
                            if (cancelled || r[0] !== true) return;
                            if (streamDisposition(r[1].state) === "processing") {
                                setProcessingState({ state: r[1].state });
                                return;
                            }
                            if (pollIntervalRef.current) {
                                clearInterval(pollIntervalRef.current);
                                pollIntervalRef.current = null;
                            }
                            setProcessingState(null);
                            setReloadCount((c) => c + 1);
                        } catch (_pollErr) {
                            // Swallow transient poll errors; the next tick will retry.
                        }
                    }, PROCESSING_POLL_INTERVAL_MS);
                    return;
                }
                setProcessingState(null);

                // 3. Viewer key for this file: the deployment's key in static mode, or one minted
                // for this asset in per-view mode. Held in memory for this open only.
                const keyResult = await mintMirisViewerKey({ databaseId, assetId, filePath });
                if (cancelled) return;
                if (keyResult[0] !== true) {
                    const [, message, keyStatus, keyState] = keyResult;
                    if (keyState === MIRIS_REQUEST_BLOCKED) {
                        setError({ kind: "REQUEST_BLOCKED", message });
                    } else if (keyStatus === 404) {
                        setStreamInfo(null);
                        setNotStreamed({ filePath: status.filePath });
                        setLoading(false);
                    } else if (keyStatus === 403) {
                        setError(
                            streamDisposition(keyState ?? "") === "notBound"
                                ? { kind: "NOT_BOUND", databaseId, assetId }
                                : { kind: "FORBIDDEN" }
                        );
                    } else if (keyStatus === 503) {
                        setError({ kind: "NOT_CONFIGURED" });
                    } else {
                        setError({ kind: "MINT_FAILED" });
                    }
                    return;
                }
                if (keyResult[1].mirisAssetUuid !== uuid) {
                    setError({ kind: "MINT_FAILED" });
                    return;
                }
                const viewerKey = keyResult[1].viewerKey;

                // 4. SDK custom elements and the device check, then the scene with this key
                try {
                    const sdkLoadTimeout = new Promise<"timeout">((resolve) => {
                        sdkLoadTimer = setTimeout(() => resolve("timeout"), SDK_LOAD_TIMEOUT_MS);
                    });
                    const sdkLoad = MirisStreamDependencyManager.loadMirisStream();
                    // A load that rejects after losing the race must not surface as unhandled.
                    sdkLoad.catch(() => undefined);
                    const outcome = await Promise.race([sdkLoad, sdkLoadTimeout]);
                    if (cancelled) return;
                    if (outcome === "timeout") {
                        MirisStreamDependencyManager.abandonPendingLoad();
                        setError({ kind: "LOAD_FAILED" });
                        return;
                    }
                } finally {
                    clearTimeout(sdkLoadTimer);
                }
                if (!MirisStreamDependencyManager.isDeviceSupported()) {
                    setError({ kind: "DEVICE_UNSUPPORTED" });
                    return;
                }
                sceneGenerationRef.current += 1;
                setScene({ uuid, viewerKey, generation: sceneGenerationRef.current });
                setLoading(false);
            } catch (e) {
                // Diagnostic logging: do NOT pass the viewer key here.
                console.error("Miris viewer load failed", {
                    assetId,
                    databaseId,
                    assetKey,
                    error: e,
                });
                if (!cancelled) setError({ kind: "LOAD_FAILED" });
            }
        }

        void load();

        return () => {
            cancelled = true;
            clearTimeout(sdkLoadTimer);
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
            }
        };
    }, [assetId, databaseId, assetKey, reloadCount]);

    // After an upload starts, the file records no stream until the pipeline writes its stream
    // metadata, so the status is polled on the processing interval until it does; load() then
    // handles the new state from the top.
    const uploadPending = uploadStarted !== null;
    useEffect(() => {
        if (!uploadPending || !assetKey) return;
        let cancelled = false;
        const filePath = toStatusFilePath(assetKey);
        const timer = setInterval(async () => {
            try {
                const r = await getMirisStreamStatus({ databaseId, assetId, filePath });
                if (cancelled || r[0] !== true) return;
                if (streamDisposition(r[1].state) === "notStreamed") return;
                clearInterval(timer);
                setUploadStarted(null);
                setReloadCount((c) => c + 1);
            } catch (_pollErr) {
                // Swallow transient poll errors; the next tick will retry.
            }
        }, PROCESSING_POLL_INTERVAL_MS);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, [uploadPending, assetId, databaseId, assetKey]);

    // Before the button is offered, the asset's executions are checked for an unfinished upload run,
    // which would otherwise be started a second time. A failed or skipped lookup offers the button.
    const notStreamedPath = notStreamed?.filePath ?? null;
    const canStartUpload =
        canExecuteWorkflow &&
        notStreamedPath !== null &&
        acceptsMirisUpload(notStreamedPath) &&
        uploadPipelineDeployed();
    const checkRunningUpload = canStartUpload && canListExecutions;
    useEffect(() => {
        setRunningUpload(null);
        if (!checkRunningUpload) return;
        let cancelled = false;
        setRunningUpload({ kind: "checking" });
        findRunningMirisUpload({ databaseId, assetId })
            .then((result) => {
                if (cancelled) return;
                setRunningUpload(
                    result[0] === true && result[1]
                        ? { kind: "running", executionId: result[1].workflowExecutionId }
                        : null
                );
            })
            .catch(() => {
                if (!cancelled) setRunningUpload(null);
            });
        return () => {
            cancelled = true;
        };
    }, [checkRunningUpload, notStreamedPath, assetId, databaseId]);

    // While a run is in progress, the status and the run are re-checked on the processing interval:
    // a recorded stream re-runs load(), and a run that has finished offers the button again.
    const uploadRunning = runningUpload?.kind === "running";
    useEffect(() => {
        if (!uploadRunning || !assetKey) return;
        let cancelled = false;
        const filePath = toStatusFilePath(assetKey);
        const timer = setInterval(async () => {
            try {
                const r = await getMirisStreamStatus({ databaseId, assetId, filePath });
                if (cancelled) return;
                if (r[0] === true && streamDisposition(r[1].state) !== "notStreamed") {
                    clearInterval(timer);
                    setRunningUpload(null);
                    setReloadCount((c) => c + 1);
                    return;
                }
                const running = await findRunningMirisUpload({ databaseId, assetId });
                if (cancelled || running[0] !== true) return;
                setRunningUpload(
                    running[1]
                        ? { kind: "running", executionId: running[1].workflowExecutionId }
                        : null
                );
            } catch (_pollErr) {
                // Swallow transient poll errors; the next tick will retry.
            }
        }, PROCESSING_POLL_INTERVAL_MS);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, [uploadRunning, assetId, databaseId, assetKey]);

    // Starts the Miris Spatial Streaming Upload workflow on the viewed file.
    async function startUpload() {
        if (!notStreamed || uploadRequest?.kind === "starting") return;
        const generation = loadGenerationRef.current;
        setUploadRequest({ kind: "starting" });
        const result = await startMirisUpload({
            databaseId,
            assetId,
            filePath: notStreamed.filePath,
        });
        if (generation !== loadGenerationRef.current) return;
        if (result[0] !== true) {
            const [, message, uploadStatus, uploadState] = result;
            setUploadRequest({
                kind: "failed",
                message:
                    uploadState !== MIRIS_REQUEST_BLOCKED && uploadStatus === 403
                        ? "You don't have permission to run the Miris upload workflow."
                        : message,
            });
            return;
        }
        setUploadRequest(null);
        setNotStreamed(null);
        setUploadStarted({ executionId: result[1].executionId || null });
    }

    // The scene element owns rendering, camera framing, resize and teardown. The stream's
    // first data clears the load timeout; the scene's `error` event reports a WebGL shader
    // failure. Any viewer error unmounts the scene, which releases its renderer. The listeners
    // attach in the commit, ahead of anything the elements dispatch once their async connect runs.
    const mountedScene = error ? null : scene;
    useLayoutEffect(() => {
        const sceneElement = sceneRef.current;
        const streamElement = streamRef.current;
        if (!mountedScene || !sceneElement || !streamElement) return;
        const loadTimer = setTimeout(
            () => setError({ kind: "STREAM_TIMEOUT", uuid: mountedScene.uuid }),
            STREAM_LOAD_TIMEOUT_MS
        );
        const onStreamLoaded = () => clearTimeout(loadTimer);
        const onSceneError = () => {
            clearTimeout(loadTimer);
            setError({ kind: "DEVICE_UNSUPPORTED" });
        };
        streamElement.addEventListener("streamloaded", onStreamLoaded);
        sceneElement.addEventListener("error", onSceneError);
        return () => {
            clearTimeout(loadTimer);
            streamElement.removeEventListener("streamloaded", onStreamLoaded);
            sceneElement.removeEventListener("error", onSceneError);
        };
    }, [mountedScene]);

    // Stale-stream check. The host passes the viewed file version as `versionId`, but it can
    // be absent for the current version (e.g. a basic-mode listing, or before the host has
    // resolved it), so the file's current S3 version is looked up instead. A historical asset
    // version without a file version id is not compared. Never blocks streaming.
    const sourceVersionId = streamInfo?.sourceVersionId;
    useEffect(() => {
        let cancelled = false;
        setStaleStream(false);
        if (!isKnownVersionId(sourceVersionId) || !assetKey) return;

        async function checkStale() {
            let viewedVersionId: string | undefined = isKnownVersionId(versionId)
                ? versionId
                : undefined;
            if (!viewedVersionId && !assetVersionId) {
                try {
                    const [ok, fileInfo] = await fetchFileInfo({
                        databaseId,
                        assetId,
                        fileKey: assetKey,
                        includeVersions: true,
                    });
                    if (ok) viewedVersionId = latestVersionIdFromFileInfo(fileInfo);
                } catch (_fileInfoErr) {
                    // Advisory only: without the current version there is nothing to compare.
                }
            }
            if (!cancelled) setStaleStream(isStaleStream(sourceVersionId, viewedVersionId));
        }

        void checkStale();
        return () => {
            cancelled = true;
        };
    }, [sourceVersionId, assetId, databaseId, assetKey, versionId, assetVersionId]);

    if (notStreamed && !error) {
        const starting = uploadRequest?.kind === "starting";
        const running = runningUpload?.kind === "running" ? runningUpload : null;
        const offerUpload = canStartUpload && !running;
        return (
            <div className={styles.guidance}>
                <div className={styles.guidanceContent}>
                    <SpaceBetween size="m">
                        <Box variant="h3">
                            {running ? "Uploading to Miris" : "Not yet streamed with Miris"}
                        </Box>
                        {!running && (
                            <Box variant="p" color="text-body-secondary">
                                This USD file has not been uploaded to Miris Spatial Streaming.
                            </Box>
                        )}
                        {running ? (
                            <Alert type="info" statusIconAriaLabel="Info">
                                <SpaceBetween size="xs">
                                    <div>
                                        A Miris upload for this {Synonyms.asset} is already running.
                                    </div>
                                    <div>
                                        Once the upload completes, Miris prepares the asset. The
                                        viewer checks the stream status every 90 seconds and
                                        refreshes automatically when it&rsquo;s ready.
                                    </div>
                                    {canViewExecution && (
                                        <Link
                                            external
                                            externalIconAriaLabel="(opens in a new tab)"
                                            href={`#${executionDetailPath(running.executionId)}`}
                                        >
                                            View execution
                                        </Link>
                                    )}
                                </SpaceBetween>
                            </Alert>
                        ) : (
                            <Box variant="p" color="text-body-secondary">
                                To stream it,{" "}
                                {offerUpload && (
                                    <>
                                        choose <strong>Stream with Miris</strong> below, or{" "}
                                    </>
                                )}
                                use the file manager&apos;s <strong>Automation</strong> action to
                                run the <strong>Miris Spatial Streaming Upload</strong> workflow on
                                it; once processing completes, you can stream it here.
                            </Box>
                        )}
                        {offerUpload && (
                            <Button
                                variant="primary"
                                loading={starting || runningUpload?.kind === "checking"}
                                onClick={() => void startUpload()}
                            >
                                Stream with Miris
                            </Button>
                        )}
                        {offerUpload && uploadRequest?.kind === "failed" && (
                            <Alert
                                type="error"
                                statusIconAriaLabel="Error"
                                action={<Button onClick={() => void startUpload()}>Retry</Button>}
                            >
                                {uploadRequest.message}
                            </Alert>
                        )}
                    </SpaceBetween>
                </div>
            </div>
        );
    }

    const streaming = !!streamInfo && !error && !loading && !processingState;

    return (
        <div className={styles.container}>
            {mountedScene && (
                <miris-scene
                    key={mountedScene.generation}
                    ref={sceneRef}
                    viewer-key={mountedScene.viewerKey}
                    camera-fit="contain"
                >
                    <miris-camera controls />
                    <miris-stream ref={streamRef} uuid={mountedScene.uuid} />
                </miris-scene>
            )}
            {error && (
                <div className={styles.errorOverlay}>
                    <div>{errorMessage(error)}</div>
                    <div className={styles.overlayAction}>
                        <Button
                            onClick={() => {
                                setScene(null);
                                setError(null);
                                setReloadCount((c) => c + 1);
                            }}
                        >
                            Retry
                        </Button>
                    </div>
                </div>
            )}
            {processingState && !error && (
                <div className={styles.errorOverlay}>
                    <div>Miris is preparing this asset.</div>
                    <div className={styles.overlayNote}>This usually takes 1&ndash;2 hours.</div>
                    <div className={styles.overlayNote}>
                        The viewer will refresh automatically when it&rsquo;s ready.
                    </div>
                    <div className={styles.overlayState}>
                        Current state: {processingState.state}
                    </div>
                </div>
            )}
            {uploadStarted && !error && (
                <div className={styles.errorOverlay}>
                    <div>Upload to Miris started.</div>
                    <div className={styles.overlayNote}>
                        Once the upload completes, Miris prepares the asset. This usually takes
                        1&ndash;2 hours.
                    </div>
                    <div className={styles.overlayNote}>
                        The viewer checks the stream status every 90 seconds and refreshes
                        automatically when it&rsquo;s ready.
                    </div>
                    {uploadStarted.executionId && canViewExecution && (
                        <div className={styles.overlayAction}>
                            <Link
                                external
                                color="inverted"
                                externalIconAriaLabel="(opens in a new tab)"
                                href={`#${executionDetailPath(uploadStarted.executionId)}`}
                            >
                                View execution
                            </Link>
                        </div>
                    )}
                </div>
            )}
            {loading && !error && !processingState && (
                <div className={styles.errorOverlay}>Loading…</div>
            )}
            {streaming && (streamInfo?.displayName || staleStream) && (
                <div className={styles.topOverlay}>
                    {streamInfo?.displayName && (
                        <div className={styles.label}>{streamInfo.displayName}</div>
                    )}
                    {staleStream && (
                        <Alert type="warning" statusIconAriaLabel="Warning">
                            This stream was built from a different version of this file. Run the
                            Miris Spatial Streaming Upload workflow again to update it.
                        </Alert>
                    )}
                </div>
            )}
        </div>
    );
};

export default MirisStreamViewerComponent;
