/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { ViewerPluginProps } from "../../core/types";
import { fetchFileInfo } from "../../../services/APIService";
import {
    getMirisStreamStatus,
    MIRIS_REQUEST_BLOCKED,
    mintMirisViewerKey,
} from "../../../services/addons/mirisAPIService";
import {
    isKnownVersionId,
    isStaleStream,
    latestVersionIdFromFileInfo,
    streamDisposition,
    toStatusFilePath,
} from "./streamStatus";
import { MirisStreamDependencyManager } from "./dependencies";
import type { MirisSceneElement, MirisStreamElement } from "./mirisElements";
import styles from "./MirisStreamViewer.module.css";

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
            return "This file's miris_asset_uuid metadata is not a valid UUID. Correct it in the file's metadata.";
        case "NOT_BOUND":
            return (
                "The Miris asset recorded for this file is not linked to this VAMS asset: it must " +
                `exist in the Miris workspace and carry the vams-asset-${err.assetId} and ` +
                `vams-database-${err.databaseId} tags. Add those tags to the Miris asset.`
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
 *   - notStreamed  -> "Not yet streamed" guidance
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
    const [notStreamed, setNotStreamed] = useState(false);
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

    useEffect(() => {
        let cancelled = false;
        let sdkLoadTimer: ReturnType<typeof setTimeout> | undefined;

        async function load() {
            try {
                if (!assetKey) {
                    setStreamInfo(null);
                    setScene(null);
                    setError(null);
                    setProcessingState(null);
                    setNotStreamed(false);
                    setLoading(false);
                    return;
                }
                setLoading(true);
                setError(null);
                setNotStreamed(false);
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
                    setNotStreamed(true);
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
                        setNotStreamed(true);
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
        return (
            <div className={styles.guidance}>
                <div className={styles.guidanceContent}>
                    <SpaceBetween size="m">
                        <Box variant="h3">Not yet streamed with Miris</Box>
                        <Box variant="p" color="text-body-secondary">
                            This USD file has not been uploaded to Miris Spatial Streaming. To view
                            it now, choose <strong>Needle USD Viewer (Experimental)</strong> in the
                            viewer selector.
                        </Box>
                        <Box variant="p" color="text-body-secondary">
                            To stream it, upload it to Miris and set the file&apos;s{" "}
                            <strong>miris_asset_uuid</strong> metadata to the Miris asset&apos;s
                            UUID; once processing completes, you can stream it here.
                        </Box>
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
                            This stream was built from a different version of this file. Upload this
                            version to Miris and update the file&apos;s stream metadata to stream
                            it.
                        </Alert>
                    )}
                </div>
            )}
        </div>
    );
};

export default MirisStreamViewerComponent;
