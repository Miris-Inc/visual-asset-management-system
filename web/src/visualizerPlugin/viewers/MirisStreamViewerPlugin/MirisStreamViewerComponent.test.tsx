/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import MirisStreamViewerComponent from "./MirisStreamViewerComponent";
import { MirisStreamDependencyManager } from "./dependencies";
import viewerConfig from "../../config/viewerConfig.json";
import addonViewerConfig from "../../config/addonViewerConfig.json";

const VALID_UUID = "063393b4-4b0d-4196-a6d4-ebcde70c56c2";

// --- Mock the dependency manager (registers the custom elements in the browser) ---
// jsdom cannot load the SDK (WASM, WebGL), so the custom elements stay undefined and render as
// plain elements whose attributes and events the tests drive directly.
jest.mock("./dependencies", () => ({
    MirisStreamDependencyManager: {
        loadMirisStream: jest.fn().mockResolvedValue(undefined),
        isDeviceSupported: jest.fn().mockReturnValue(true),
        abandonPendingLoad: jest.fn(),
        cleanup: jest.fn(),
    },
}));
const mockLoadMirisStream = MirisStreamDependencyManager.loadMirisStream as jest.Mock;
const mockAbandonPendingLoad = MirisStreamDependencyManager.abandonPendingLoad as jest.Mock;
const mockIsDeviceSupported = MirisStreamDependencyManager.isDeviceSupported as jest.Mock;

// --- Mock the stream status, viewer key and file info calls ---
const mockGetMirisStreamStatus = jest.fn();
const mockFetchFileInfo = jest.fn();
const mockDownloadAsset = jest.fn();
const mockMintMirisViewerKey = jest.fn();
const mockStartMirisUpload = jest.fn();
const mockFindRunningMirisUpload = jest.fn();
jest.mock("../../../services/APIService", () => ({
    fetchFileInfo: (...args: unknown[]) => mockFetchFileInfo(...args),
    downloadAsset: (...args: unknown[]) => mockDownloadAsset(...args),
}));
jest.mock("../../../services/addons/mirisAPIService", () => ({
    ...jest.requireActual("../../../services/addons/mirisAPIService"),
    getMirisStreamStatus: (...args: unknown[]) => mockGetMirisStreamStatus(...args),
    mintMirisViewerKey: (...args: unknown[]) => mockMintMirisViewerKey(...args),
    startMirisUpload: (...args: unknown[]) => mockStartMirisUpload(...args),
    findRunningMirisUpload: (...args: unknown[]) => mockFindRunningMirisUpload(...args),
}));

// --- Mock the Tier-1 route check ---
const EXECUTE_ROUTE = "/workflows/{workflowDatabaseId}/{workflowId}/execute";
const EXECUTION_DETAILS_ROUTE = "/workflows/executions/{executionId}/details";
const ASSET_EXECUTIONS_ROUTE = "/database/{databaseId}/assets/{assetId}/workflows/executions";
const mockCan = jest.fn();
jest.mock("../../../features/orchestration/permissions/useAllowedRoutes", () => ({
    useAllowedRoutes: () => ({
        loading: false,
        can: (...args: unknown[]) => mockCan(...args),
    }),
}));

// --- Mock appCache ---
const mockAppCacheGet = jest.fn();
const mockAppCacheSet = jest.fn();
jest.mock("../../../services/appCache", () => ({
    appCache: {
        getItem: (key: string) => mockAppCacheGet(key),
        setItem: (...args: unknown[]) => mockAppCacheSet(...args),
    },
}));

const status = (overrides: Record<string, unknown> = {}) => [
    true,
    {
        filePath: "/folder/scene.usd",
        mirisAssetUuid: VALID_UUID,
        sourceVersionId: "v1",
        uploadedAt: "2026-10-14T12:00:00Z",
        displayName: "Scene stream",
        state: "streamable",
        ...overrides,
    },
];

// The static-mode key, as the viewer key route returns it: the deployment's key, with no expiry.
const STATIC_KEY = "key-1234567890abcdef";
const staticKey = (overrides: Record<string, unknown> = {}) => [
    true,
    {
        filePath: "/asset-1/folder/scene.usd",
        mirisAssetUuid: VALID_UUID,
        viewerKey: STATIC_KEY,
        expiresAt: null,
        ...overrides,
    },
];

beforeEach(() => {
    jest.clearAllMocks();
    mockAppCacheGet.mockReturnValue({ mirisViewerKey: "secure-config-key", featuresEnabled: [] });
    mockGetMirisStreamStatus.mockResolvedValue(status());
    mockMintMirisViewerKey.mockResolvedValue(staticKey());
    mockCan.mockReturnValue(false);
    mockFindRunningMirisUpload.mockResolvedValue([true, null]);
    mockFetchFileInfo.mockResolvedValue([
        true,
        { versions: [{ versionId: "v1", isLatest: true }] },
    ]);
});

const baseProps = {
    assetId: "asset-1",
    databaseId: "db-1",
    assetKey: "asset-1/folder/scene.usd",
    versionId: "v1",
    viewerMode: "wide",
    onViewerModeChange: jest.fn(),
};

const STALE_TEXT = /built from a different version of this file/i;

// secure-config of a deployment that includes the Miris upload pipeline.
const withUploadPipeline = () =>
    mockAppCacheGet.mockReturnValue({
        mirisViewerKey: "secure-config-key",
        featuresEnabled: ["MIRIS_ADDON", "MIRIS_UPLOAD"],
    });

const viewerEntry = (id: string) =>
    [
        ...(viewerConfig as { viewers: Array<Record<string, any>> }).viewers,
        ...(addonViewerConfig as { viewers: Array<Record<string, any>> }).viewers,
    ].find((viewer) => viewer.id === id) as Record<string, any>;

const retryButton = () => screen.getByRole("button", { name: "Retry" });

const sceneElement = () => document.querySelector("miris-scene");
const streamElement = () => document.querySelector("miris-stream");
const waitForScene = async (): Promise<Element> => {
    await waitFor(() => expect(sceneElement()).not.toBeNull());
    return sceneElement() as Element;
};

describe("MirisStreamViewerComponent", () => {
    describe("status request", () => {
        it("asks for the status of the viewed file by its object key", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1));
            expect(mockGetMirisStreamStatus).toHaveBeenCalledWith({
                databaseId: "db-1",
                assetId: "asset-1",
                filePath: "/asset-1/folder/scene.usd",
            });
        });

        it("sends a key under a bucket prefix unchanged for the server to reduce", async () => {
            render(
                <MirisStreamViewerComponent
                    {...baseProps}
                    assetKey="projects/site-a/sub/scene.usd"
                />
            );
            await waitFor(() => expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1));
            expect(mockGetMirisStreamStatus.mock.calls[0][0].filePath).toBe(
                "/projects/site-a/sub/scene.usd"
            );
        });

        it("never downloads the file", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(mockDownloadAsset).not.toHaveBeenCalled();
        });
    });

    describe("streaming", () => {
        it("renders the scene with the viewer key and exactly the returned UUID", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            const scene = await waitForScene();
            expect(scene).toHaveAttribute("viewer-key", STATIC_KEY);
            expect(scene).toHaveAttribute("camera-fit", "contain");
            expect(scene.querySelectorAll("miris-camera")).toHaveLength(1);
            expect(scene.querySelectorAll("miris-stream")).toHaveLength(1);
            expect(streamElement()).toHaveAttribute("uuid", VALID_UUID);
        });

        it("orbits the camera and leaves the asset fixed", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            const scene = await waitForScene();
            const camera = scene.querySelector("miris-camera");
            // The elements treat any present `controls` attribute as enabled.
            expect(camera).toHaveAttribute("controls");
            expect(camera?.getAttribute("controls")).not.toBe("false");
            // Stream controls rotate the asset about its own origin on the same drag that
            // orbits the camera, so the stream carries no `controls` attribute at all.
            expect(streamElement()).not.toHaveAttribute("controls");
        });

        it("registers the SDK elements before rendering the scene", async () => {
            let finishLoad: () => void = () => undefined;
            mockLoadMirisStream.mockReturnValueOnce(
                new Promise<void>((resolve) => {
                    finishLoad = resolve;
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockLoadMirisStream).toHaveBeenCalledTimes(1));
            expect(sceneElement()).toBeNull();
            await act(async () => {
                finishLoad();
            });
            await waitForScene();
        });

        it("shows the load error when the SDK fails to load", async () => {
            mockLoadMirisStream.mockRejectedValueOnce(new Error("chunk"));
            const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/miris viewer failed to load/i)).toBeInTheDocument()
            );
            expect(sceneElement()).toBeNull();
            consoleError.mockRestore();
        });

        it("labels the stream with the returned display name", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText("Scene stream")).toBeInTheDocument());
        });

        it("streams when Miris could not be queried (state unknown)", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "unknown" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(streamElement()).toHaveAttribute("uuid", VALID_UUID);
            expect(screen.queryByText(/preparing this asset/i)).not.toBeInTheDocument();
        });

        it("shows the not-configured error when the viewer key route answers 503", async () => {
            mockMintMirisViewerKey.mockResolvedValue([false, "not configured", 503, undefined]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/Miris streaming is not configured/i)).toBeInTheDocument()
            );
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
        });

        it("removes the scene when the viewed file is cleared", async () => {
            const view = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();

            view.rerender(<MirisStreamViewerComponent {...baseProps} assetKey="" />);
            await waitFor(() => expect(sceneElement()).toBeNull());
            expect(screen.queryByText("Scene stream")).not.toBeInTheDocument();
        });

        it("removes the scene element on unmount", async () => {
            const { unmount } = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();

            await act(async () => {
                unmount();
            });
            expect(sceneElement()).toBeNull();
        });
    });

    describe("load events", () => {
        afterEach(() => {
            jest.useRealTimers();
        });

        it("times out when the stream sends no data", async () => {
            jest.useFakeTimers();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();

            await act(async () => {
                jest.advanceTimersByTime(60_000);
            });
            expect(screen.getByText(/timed out waiting for miris/i)).toBeInTheDocument();
            expect(screen.getByText(new RegExp(VALID_UUID))).toBeInTheDocument();
            expect(sceneElement()).toBeNull();
        });

        it("clears the timeout when the stream reports streamloaded", async () => {
            jest.useFakeTimers();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();

            act(() => {
                streamElement()?.dispatchEvent(new CustomEvent("streamloaded"));
            });
            await act(async () => {
                jest.advanceTimersByTime(120_000);
            });
            expect(screen.queryByText(/timed out waiting for miris/i)).not.toBeInTheDocument();
            expect(sceneElement()).not.toBeNull();
        });

        it("clears the timeout on unmount", async () => {
            jest.useFakeTimers();
            const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
            const { unmount } = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            unmount();
            await act(async () => {
                jest.advanceTimersByTime(60_000);
            });
            expect(jest.getTimerCount()).toBe(0);
            consoleError.mockRestore();
        });

        it("fails at once without mounting a scene when the device cannot render", async () => {
            jest.useFakeTimers();
            mockIsDeviceSupported.mockReturnValueOnce(false);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/not supported on this device/i)).toBeInTheDocument()
            );
            expect(screen.getByText(/GES\) need WebGPU/)).toBeInTheDocument();
            expect(screen.getByText(/WebGPU-capable browser/)).toBeInTheDocument();
            expect(mockLoadMirisStream).toHaveBeenCalledTimes(1);
            expect(sceneElement()).toBeNull();
            expect(streamElement()).toBeNull();
            expect(jest.getTimerCount()).toBe(0);
        });

        it("shows the unsupported-device error when the scene reports an error", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            const scene = await waitForScene();

            act(() => {
                scene.dispatchEvent(new Event("error"));
            });
            expect(screen.getByText(/not supported on this device/i)).toBeInTheDocument();
            expect(sceneElement()).toBeNull();
        });
    });

    describe("SDK load", () => {
        afterEach(() => {
            jest.useRealTimers();
        });

        const neverSettles = () =>
            mockLoadMirisStream.mockReturnValueOnce(new Promise<void>(() => undefined));
        const waitForSdkLoad = (calls: number) =>
            waitFor(() => expect(mockLoadMirisStream).toHaveBeenCalledTimes(calls));

        it("shows the load error and mounts no scene when the SDK load never settles", async () => {
            jest.useFakeTimers();
            neverSettles();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForSdkLoad(1);
            expect(screen.getByText("Loading…")).toBeInTheDocument();

            await act(async () => {
                jest.advanceTimersByTime(60_000);
            });
            expect(screen.getByText(/miris viewer failed to load/i)).toBeInTheDocument();
            expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
            expect(sceneElement()).toBeNull();
            expect(mockAbandonPendingLoad).toHaveBeenCalledTimes(1);
            expect(jest.getTimerCount()).toBe(0);
        });

        it("does not time out a load that settles in time", async () => {
            jest.useFakeTimers();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(mockAbandonPendingLoad).not.toHaveBeenCalled();
            expect(screen.queryByText(/miris viewer failed to load/i)).not.toBeInTheDocument();
        });

        it("shows nothing and leaves no timers when unmounted during the wait", async () => {
            jest.useFakeTimers();
            neverSettles();
            const { unmount } = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForSdkLoad(1);
            unmount();
            expect(jest.getTimerCount()).toBe(0);
            await act(async () => {
                jest.advanceTimersByTime(120_000);
            });
            expect(mockAbandonPendingLoad).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
        });

        it("ignores the timeout of an open the file change replaced", async () => {
            jest.useFakeTimers();
            neverSettles();
            const { rerender } = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForSdkLoad(1);
            rerender(<MirisStreamViewerComponent {...baseProps} assetKey="asset-1/other.usd" />);
            await waitForScene();
            await act(async () => {
                jest.advanceTimersByTime(120_000);
            });
            expect(screen.queryByText(/miris viewer failed to load/i)).not.toBeInTheDocument();
            expect(mockAbandonPendingLoad).not.toHaveBeenCalled();
        });

        it("calls into the loader again on a later open after a timed-out load", async () => {
            jest.useFakeTimers();
            neverSettles();
            const first = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForSdkLoad(1);
            await act(async () => {
                jest.advanceTimersByTime(60_000);
            });
            first.unmount();
            expect(mockAbandonPendingLoad).toHaveBeenCalledTimes(1);

            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(mockLoadMirisStream).toHaveBeenCalledTimes(2);
        });
    });

    describe("file cleared", () => {
        it("resets every overlay when the asset key becomes empty", async () => {
            mockGetMirisStreamStatus.mockResolvedValueOnce([false, "Not Authorized"]);
            const { rerender, container } = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(
                    screen.getByText(/could not load the miris stream status/i)
                ).toBeInTheDocument()
            );
            rerender(<MirisStreamViewerComponent {...baseProps} assetKey="" />);
            await waitFor(() =>
                expect(
                    screen.queryByText(/could not load the miris stream status/i)
                ).not.toBeInTheDocument()
            );
            expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
            expect(container.textContent).toBe("");
        });

        it("clears the not-streamed guidance and the processing overlay", async () => {
            mockGetMirisStreamStatus.mockResolvedValueOnce(
                status({ state: "notStreamed", mirisAssetUuid: null })
            );
            const first = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/not yet streamed with miris/i)).toBeInTheDocument()
            );
            first.rerender(<MirisStreamViewerComponent {...baseProps} assetKey="" />);
            await waitFor(() =>
                expect(screen.queryByText(/not yet streamed with miris/i)).not.toBeInTheDocument()
            );
            first.unmount();

            mockGetMirisStreamStatus.mockResolvedValueOnce(status({ state: "processing" }));
            const second = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/miris is preparing this asset/i)).toBeInTheDocument()
            );
            second.rerender(<MirisStreamViewerComponent {...baseProps} assetKey="" />);
            await waitFor(() =>
                expect(screen.queryByText(/miris is preparing this asset/i)).not.toBeInTheDocument()
            );
            expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
        });
    });

    describe("not streamed", () => {
        it("shows the Not yet streamed guidance and does not touch the SDK", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(
                status({
                    mirisAssetUuid: null,
                    sourceVersionId: null,
                    uploadedAt: null,
                    displayName: null,
                    state: "notStreamed",
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/not yet streamed with miris/i)).toBeInTheDocument()
            );
            expect(screen.getByText("Miris Spatial Streaming Upload")).toBeInTheDocument();
            expect(screen.queryByRole("button")).not.toBeInTheDocument();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
            expect(streamElement()).toBeNull();
        });

        it("points to the Needle USD viewer, by its configured name, to view the file now", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(
                status({ mirisAssetUuid: null, state: "notStreamed" })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            const needleName = viewerEntry("needletools-usd-viewer").name;
            await waitFor(() => expect(screen.getByText(needleName)).toBeInTheDocument());
        });

        it("is not preceded by a registry-driven SDK load", () => {
            // With a dependencyManagerMethod the registry awaits the SDK, without a bound, before
            // the viewer mounts, and loads it even for a file that is not streamed.
            const entry = viewerEntry("miris-stream-viewer");
            expect(entry.dependencyManagerMethod).toBeUndefined();
            expect(entry.dependencyCleanupMethod).toBe("cleanup");
        });

        it("asks for no viewer key for a file that is not streamed", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(
                status({ mirisAssetUuid: null, state: "notStreamed" })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/not yet streamed with miris/i)).toBeInTheDocument()
            );
            expect(mockMintMirisViewerKey).not.toHaveBeenCalled();
        });
    });

    describe("stream with miris", () => {
        const NOT_STREAMED = status({
            mirisAssetUuid: null,
            sourceVersionId: null,
            uploadedAt: null,
            displayName: null,
            state: "notStreamed",
        });
        const streamButton = () => screen.queryByRole("button", { name: "Stream with Miris" });
        const clickStreamButton = () =>
            act(() => {
                screen.getByRole("button", { name: "Stream with Miris" }).click();
            });
        const waitForGuidance = () =>
            waitFor(() =>
                expect(screen.getByText(/not yet streamed with miris/i)).toBeInTheDocument()
            );
        const allowRoutes = (...allowed: string[]) =>
            mockCan.mockImplementation((_method: string, path: string) => allowed.includes(path));

        beforeEach(() => {
            withUploadPipeline();
            mockGetMirisStreamStatus.mockResolvedValue(NOT_STREAMED);
            allowRoutes(EXECUTE_ROUTE, EXECUTION_DETAILS_ROUTE);
            mockStartMirisUpload.mockResolvedValue([true, { executionId: "exec-1" }]);
        });

        afterEach(() => {
            jest.useRealTimers();
        });

        it("offers the button to a user allowed to execute workflows", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).toBeInTheDocument();
            expect(mockCan).toHaveBeenCalledWith("POST", EXECUTE_ROUTE);
            // The Needle and Automation guidance stays beside the button.
            expect(screen.getByText("Automation")).toBeInTheDocument();
            expect(screen.getByText("Miris Spatial Streaming Upload")).toBeInTheDocument();
        });

        it("hides the button when the deployment does not include the upload pipeline", async () => {
            mockAppCacheGet.mockReturnValue({
                mirisViewerKey: "secure-config-key",
                featuresEnabled: ["MIRIS_ADDON"],
            });
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).not.toBeInTheDocument();
            // Neither the button nor the guidance that points to it.
            expect(screen.queryByText("Stream with Miris")).not.toBeInTheDocument();
            // The Needle and Automation guidance remains.
            expect(screen.getByText("Automation")).toBeInTheDocument();
            expect(screen.getByText("Miris Spatial Streaming Upload")).toBeInTheDocument();
            expect(mockFindRunningMirisUpload).not.toHaveBeenCalled();
        });

        it("hides the button when secure-config carries no feature list", async () => {
            mockAppCacheGet.mockReturnValue({ mirisViewerKey: "secure-config-key" });
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).not.toBeInTheDocument();
            expect(screen.getByText("Automation")).toBeInTheDocument();
        });

        it("hides the button from a user not allowed to execute workflows", async () => {
            allowRoutes(EXECUTION_DETAILS_ROUTE);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).not.toBeInTheDocument();
            expect(screen.getByText("Automation")).toBeInTheDocument();
        });

        it("hides the button for a file type the upload workflow does not accept", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(
                status({ mirisAssetUuid: null, state: "notStreamed", filePath: "/folder/a.glb" })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).not.toBeInTheDocument();
        });

        it("starts the upload for the viewed file by its asset-relative path", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() => expect(mockStartMirisUpload).toHaveBeenCalledTimes(1));
            expect(mockStartMirisUpload).toHaveBeenCalledWith({
                databaseId: "db-1",
                assetId: "asset-1",
                filePath: "/folder/scene.usd",
            });
        });

        it("disables the button while the request is in flight", async () => {
            let resolveStart: (value: unknown) => void = () => undefined;
            mockStartMirisUpload.mockReturnValue(
                new Promise((resolve) => {
                    resolveStart = resolve;
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() => expect(streamButton()).toHaveAttribute("aria-disabled", "true"));
            clickStreamButton();
            expect(mockStartMirisUpload).toHaveBeenCalledTimes(1);

            await act(async () => {
                resolveStart([true, { executionId: "exec-1" }]);
            });
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
        });

        it("shows the started state with a link to the execution", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
            expect(screen.queryByText(/not yet streamed with miris/i)).not.toBeInTheDocument();
            expect(
                screen.getByText(/checks the stream status every 90 seconds/i)
            ).toBeInTheDocument();
            expect(screen.getByRole("link", { name: /view execution/i })).toHaveAttribute(
                "href",
                "#/executions/exec-1"
            );
        });

        it("links no execution when the response names none", async () => {
            mockStartMirisUpload.mockResolvedValue([true, {}]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
            expect(screen.queryByRole("link", { name: /view execution/i })).not.toBeInTheDocument();
        });

        it("links no execution for a user who may not read execution details", async () => {
            allowRoutes(EXECUTE_ROUTE);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
            expect(screen.queryByRole("link", { name: /view execution/i })).not.toBeInTheDocument();
        });

        it("polls the status every 90 seconds and moves to processing once the stream is recorded", async () => {
            jest.useFakeTimers();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1);

            // Still not recorded: the started state stays and the poll continues.
            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(2);
            expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument();

            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "preview" }));
            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });
            await waitFor(() =>
                expect(screen.getByText(/preparing this asset/i)).toBeInTheDocument()
            );
            expect(screen.queryByText(/upload to miris started/i)).not.toBeInTheDocument();
            expect(screen.getByText(/current state: preview/i)).toBeInTheDocument();
        });

        it("shows a permission message on a 403 and starts again on Retry", async () => {
            mockStartMirisUpload
                .mockResolvedValueOnce([false, "Forbidden", 403, undefined])
                .mockResolvedValueOnce([true, { executionId: "exec-2" }]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(
                    screen.getByText("You don't have permission to run the Miris upload workflow.")
                ).toBeInTheDocument()
            );

            act(() => {
                retryButton().click();
            });
            await waitFor(() =>
                expect(screen.getByText(/upload to miris started/i)).toBeInTheDocument()
            );
            expect(mockStartMirisUpload).toHaveBeenCalledTimes(2);
        });

        it("shows the error message for any other failure", async () => {
            mockStartMirisUpload.mockResolvedValue([
                false,
                "A conflicting execution of this workflow is already running.",
                400,
                undefined,
            ]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(
                    screen.getByText("A conflicting execution of this workflow is already running.")
                ).toBeInTheDocument()
            );
            expect(retryButton()).toBeInTheDocument();
            expect(streamButton()).toBeInTheDocument();
        });

        it("shows the blocking message, not a permission message, for a WAF-blocked request", async () => {
            mockStartMirisUpload.mockResolvedValue([
                false,
                "Request blocked (request ID abc-123)",
                undefined,
                "requestBlocked",
            ]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            clickStreamButton();
            await waitFor(() =>
                expect(screen.getByText("Request blocked (request ID abc-123)")).toBeInTheDocument()
            );
        });

        it.each([
            ["streamable", status()],
            ["processing", status({ state: "preview" })],
            ["invalidUuid", status({ mirisAssetUuid: "nope", state: "invalidUuid" })],
        ])("offers no button in the %s state", async (_label, result) => {
            mockGetMirisStreamStatus.mockResolvedValue(result);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockGetMirisStreamStatus).toHaveBeenCalled());
            await waitFor(() => expect(screen.queryByText("Loading…")).not.toBeInTheDocument());
            expect(streamButton()).not.toBeInTheDocument();
        });
    });

    describe("upload already running", () => {
        const NOT_STREAMED = status({
            mirisAssetUuid: null,
            sourceVersionId: null,
            uploadedAt: null,
            displayName: null,
            state: "notStreamed",
        });
        const RUNNING_TEXT = /a miris upload for this asset is already running/i;
        const running = (executionId = "exec-running") => [
            true,
            {
                workflowExecutionId: executionId,
                workflowId: "miris-upload",
                workflowDatabaseId: "GLOBAL",
                executionStatus: "RUNNING",
            },
        ];
        const streamButton = () => screen.queryByRole("button", { name: "Stream with Miris" });
        const waitForGuidance = () =>
            waitFor(() =>
                expect(screen.getByText(/not yet streamed with miris/i)).toBeInTheDocument()
            );
        const allowRoutes = (...allowed: string[]) =>
            mockCan.mockImplementation((_method: string, path: string) => allowed.includes(path));

        beforeEach(() => {
            withUploadPipeline();
            mockGetMirisStreamStatus.mockResolvedValue(NOT_STREAMED);
            allowRoutes(EXECUTE_ROUTE, EXECUTION_DETAILS_ROUTE, ASSET_EXECUTIONS_ROUTE);
        });

        afterEach(() => {
            jest.useRealTimers();
        });

        it("hides the button and links the run while an upload on the asset is in progress", async () => {
            mockFindRunningMirisUpload.mockResolvedValue(running());
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(RUNNING_TEXT)).toBeInTheDocument());
            expect(mockFindRunningMirisUpload).toHaveBeenCalledWith({
                databaseId: "db-1",
                assetId: "asset-1",
            });
            expect(mockCan).toHaveBeenCalledWith("GET", ASSET_EXECUTIONS_ROUTE);
            expect(streamButton()).not.toBeInTheDocument();
            expect(screen.queryByText("Automation")).not.toBeInTheDocument();
            expect(
                screen.getByText(/checks the stream status every 90 seconds/i)
            ).toBeInTheDocument();
            expect(screen.getByRole("link", { name: /view execution/i })).toHaveAttribute(
                "href",
                "#/executions/exec-running"
            );
        });

        it("links no execution for a user who may not read execution details", async () => {
            allowRoutes(EXECUTE_ROUTE, ASSET_EXECUTIONS_ROUTE);
            mockFindRunningMirisUpload.mockResolvedValue(running());
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(RUNNING_TEXT)).toBeInTheDocument());
            expect(screen.queryByRole("link", { name: /view execution/i })).not.toBeInTheDocument();
        });

        it("offers the button when no upload run is in progress", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            await waitFor(() => expect(mockFindRunningMirisUpload).toHaveBeenCalledTimes(1));
            await waitFor(() =>
                expect(streamButton()).not.toHaveAttribute("aria-disabled", "true")
            );
            expect(screen.queryByText(RUNNING_TEXT)).not.toBeInTheDocument();
        });

        it("disables the button while the lookup is in flight", async () => {
            let resolveLookup: (value: unknown) => void = () => undefined;
            mockFindRunningMirisUpload.mockReturnValue(
                new Promise((resolve) => {
                    resolveLookup = resolve;
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            await waitFor(() => expect(streamButton()).toHaveAttribute("aria-disabled", "true"));
            await act(async () => {
                resolveLookup([true, null]);
            });
            await waitFor(() =>
                expect(streamButton()).not.toHaveAttribute("aria-disabled", "true")
            );
        });

        it("offers the button when the lookup fails", async () => {
            mockFindRunningMirisUpload.mockResolvedValue([false, "Request failed"]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            await waitFor(() => expect(mockFindRunningMirisUpload).toHaveBeenCalledTimes(1));
            await waitFor(() =>
                expect(streamButton()).not.toHaveAttribute("aria-disabled", "true")
            );
            expect(screen.queryByText(RUNNING_TEXT)).not.toBeInTheDocument();
        });

        it("skips the lookup for a user who may not list the asset's executions", async () => {
            allowRoutes(EXECUTE_ROUTE, EXECUTION_DETAILS_ROUTE);
            mockFindRunningMirisUpload.mockResolvedValue(running());
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).toBeInTheDocument();
            expect(streamButton()).not.toHaveAttribute("aria-disabled", "true");
            expect(mockFindRunningMirisUpload).not.toHaveBeenCalled();
        });

        it("skips the lookup when the button would not be offered", async () => {
            allowRoutes(EXECUTION_DETAILS_ROUTE, ASSET_EXECUTIONS_ROUTE);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForGuidance();
            expect(streamButton()).not.toBeInTheDocument();
            expect(mockFindRunningMirisUpload).not.toHaveBeenCalled();
        });

        it("offers the button again once the run finishes without recording a stream", async () => {
            jest.useFakeTimers();
            mockFindRunningMirisUpload.mockResolvedValue(running());
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(RUNNING_TEXT)).toBeInTheDocument());
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1);

            // Still running: the notice stays and both checks repeat.
            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(2);
            expect(mockFindRunningMirisUpload).toHaveBeenCalledTimes(2);
            expect(screen.getByText(RUNNING_TEXT)).toBeInTheDocument();

            mockFindRunningMirisUpload.mockResolvedValue([true, null]);
            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });
            await waitFor(() => expect(streamButton()).toBeInTheDocument());
            expect(screen.queryByText(RUNNING_TEXT)).not.toBeInTheDocument();
            expect(screen.getByText("Automation")).toBeInTheDocument();
        });

        it("moves to processing once the run records the stream", async () => {
            jest.useFakeTimers();
            mockFindRunningMirisUpload.mockResolvedValue(running());
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(RUNNING_TEXT)).toBeInTheDocument());

            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "preview" }));
            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });
            await waitFor(() =>
                expect(screen.getByText(/preparing this asset/i)).toBeInTheDocument()
            );
            expect(screen.queryByText(RUNNING_TEXT)).not.toBeInTheDocument();
            expect(screen.getByText(/current state: preview/i)).toBeInTheDocument();
            expect(mockFindRunningMirisUpload).toHaveBeenCalledTimes(1);
        });
    });

    describe("not bound", () => {
        const NOT_BOUND_TEXT = /not linked to this VAMS asset/i;

        it("shows the not-linked error when the status reports notBound", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "notBound" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(NOT_BOUND_TEXT)).toBeInTheDocument());
            expect(
                screen.getByText(/vams-asset-asset-1 and vams-database-db-1/)
            ).toBeInTheDocument();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            expect(mockMintMirisViewerKey).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
        });

        it("shows the not-linked error when the mint is refused because the asset is not bound", async () => {
            mockMintMirisViewerKey.mockResolvedValue([false, "not bound", 403, "notBound"]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(NOT_BOUND_TEXT)).toBeInTheDocument());
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1);
            expect(
                screen.queryByText("You do not have permission to stream this asset.")
            ).not.toBeInTheDocument();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
        });
    });

    describe("retry", () => {
        afterEach(() => {
            jest.useRealTimers();
        });

        it("re-runs the status check and load from an error", async () => {
            mockGetMirisStreamStatus.mockResolvedValueOnce([false, "Service Unavailable"]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(
                    screen.getByText(/could not load the miris stream status/i)
                ).toBeInTheDocument()
            );
            expect(mockLoadMirisStream).not.toHaveBeenCalled();

            act(() => {
                retryButton().click();
            });
            await waitForScene();
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(2);
            expect(mockLoadMirisStream).toHaveBeenCalledTimes(1);
            expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
        });

        it("mounts a new scene after a stream timeout", async () => {
            jest.useFakeTimers();
            render(<MirisStreamViewerComponent {...baseProps} />);
            const first = await waitForScene();
            await act(async () => {
                jest.advanceTimersByTime(60_000);
            });
            expect(screen.getByText(/timed out waiting for miris/i)).toBeInTheDocument();

            act(() => {
                retryButton().click();
            });
            const second = await waitForScene();
            expect(second).not.toBe(first);
            expect(screen.queryByText(/timed out waiting for miris/i)).not.toBeInTheDocument();
        });
    });

    describe("errors", () => {
        it("shows an error for an invalid UUID without constructing the SDK", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(
                status({ mirisAssetUuid: "not-a-uuid", state: "invalidUuid" })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(screen.getByText(/not a valid UUID/i)).toBeInTheDocument());
            expect(sceneElement()).toBeNull();
            expect(streamElement()).toBeNull();
        });

        it("shows an error when the status request fails", async () => {
            mockGetMirisStreamStatus.mockResolvedValue([false, "Not Authorized"]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(
                    screen.getByText(/could not load the miris stream status/i)
                ).toBeInTheDocument()
            );
            expect(streamElement()).toBeNull();
        });

        it("shows a permission error when the status request is denied", async () => {
            mockGetMirisStreamStatus.mockResolvedValue([false, "Forbidden", 403, undefined]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(
                    screen.getByText("You do not have permission to stream this asset.")
                ).toBeInTheDocument()
            );
            expect(
                screen.queryByText(/could not load the miris stream status/i)
            ).not.toBeInTheDocument();
            expect(mockMintMirisViewerKey).not.toHaveBeenCalled();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
        });

        it("shows the blocked request's message when the status request is blocked", async () => {
            mockGetMirisStreamStatus.mockResolvedValue([
                false,
                "Request blocked (request ID abc-123)",
                undefined,
                "requestBlocked",
            ]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText("Request blocked (request ID abc-123)")).toBeInTheDocument()
            );
            expect(
                screen.queryByText("You do not have permission to stream this asset.")
            ).not.toBeInTheDocument();
            expect(
                screen.queryByText(/could not load the miris stream status/i)
            ).not.toBeInTheDocument();
            expect(mockMintMirisViewerKey).not.toHaveBeenCalled();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
        });

        it("shows an error when the status request throws", async () => {
            mockGetMirisStreamStatus.mockRejectedValue(new Error("network"));
            const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/miris viewer failed to load/i)).toBeInTheDocument()
            );
            expect(streamElement()).toBeNull();
            consoleError.mockRestore();
        });

        it("surfaces a definitive Miris processing failure", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "failed" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/miris processing failed/i)).toBeInTheDocument()
            );
            expect(sceneElement()).toBeNull();
        });
    });

    describe("processing", () => {
        afterEach(() => {
            jest.useRealTimers();
        });

        it("shows the processing overlay while Miris is still preparing the asset", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ state: "preview" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/preparing this asset/i)).toBeInTheDocument()
            );
            expect(screen.getByText(/current state: preview/i)).toBeInTheDocument();
            expect(sceneElement()).toBeNull();
        });

        it("starts streaming once the poll reports the asset streamable", async () => {
            jest.useFakeTimers();
            mockGetMirisStreamStatus
                .mockResolvedValueOnce(status({ state: "preview" }))
                .mockResolvedValue(status({ state: "streamable" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/preparing this asset/i)).toBeInTheDocument()
            );

            await act(async () => {
                jest.advanceTimersByTime(90_000);
            });

            await waitFor(() => expect(streamElement()).toHaveAttribute("uuid", VALID_UUID));
            expect(screen.queryByText(/preparing this asset/i)).not.toBeInTheDocument();
        });
    });

    describe("stale banner", () => {
        it("shows the banner when the stream was built from a different version", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ sourceVersionId: "v0" }));
            render(<MirisStreamViewerComponent {...baseProps} versionId="v1" />);
            await waitFor(() => expect(screen.getByText(STALE_TEXT)).toBeInTheDocument());
            // Still streaming.
            expect(streamElement()).toHaveAttribute("uuid", VALID_UUID);
            expect(mockFetchFileInfo).not.toHaveBeenCalled();
        });

        it("shows no banner when the versions match", async () => {
            render(<MirisStreamViewerComponent {...baseProps} versionId="v1" />);
            await waitForScene();
            expect(screen.queryByText(STALE_TEXT)).not.toBeInTheDocument();
        });

        it.each([[""], [null]])(
            "shows no banner when the source version is %p",
            async (sourceVersionId) => {
                mockGetMirisStreamStatus.mockResolvedValue(status({ sourceVersionId }));
                render(<MirisStreamViewerComponent {...baseProps} versionId="v1" />);
                await waitForScene();
                expect(screen.queryByText(STALE_TEXT)).not.toBeInTheDocument();
                expect(mockFetchFileInfo).not.toHaveBeenCalled();
            }
        );

        it("compares against the file's current version when no versionId is passed", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ sourceVersionId: "v1" }));
            mockFetchFileInfo.mockResolvedValue([
                true,
                {
                    versions: [
                        { versionId: "v1", isLatest: false },
                        { versionId: "v2", isLatest: true },
                    ],
                },
            ]);
            render(<MirisStreamViewerComponent {...baseProps} versionId={undefined} />);
            await waitFor(() => expect(screen.getByText(STALE_TEXT)).toBeInTheDocument());
            expect(mockFetchFileInfo).toHaveBeenCalledWith({
                databaseId: "db-1",
                assetId: "asset-1",
                fileKey: "asset-1/folder/scene.usd",
                includeVersions: true,
            });
        });

        it("shows no banner when the current version matches the source", async () => {
            render(<MirisStreamViewerComponent {...baseProps} versionId={undefined} />);
            await waitFor(() => expect(mockFetchFileInfo).toHaveBeenCalledTimes(1));
            await waitForScene();
            expect(screen.queryByText(STALE_TEXT)).not.toBeInTheDocument();
        });

        it("keeps streaming without a banner when the file info lookup fails", async () => {
            mockGetMirisStreamStatus.mockResolvedValue(status({ sourceVersionId: "v0" }));
            mockFetchFileInfo.mockResolvedValue([false, "Not Authorized"]);
            render(<MirisStreamViewerComponent {...baseProps} versionId={undefined} />);
            await waitFor(() => expect(mockFetchFileInfo).toHaveBeenCalledTimes(1));
            await waitForScene();
            expect(screen.queryByText(STALE_TEXT)).not.toBeInTheDocument();
        });
    });

    describe("viewer key", () => {
        it("asks the viewer key route once for the viewed file", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(1);
            expect(mockMintMirisViewerKey).toHaveBeenCalledWith({
                databaseId: "db-1",
                assetId: "asset-1",
                filePath: "/asset-1/folder/scene.usd",
            });
        });

        it("never reads a viewer key from the app config", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            const scene = await waitForScene();
            expect(scene).toHaveAttribute("viewer-key", STATIC_KEY);
            expect(mockAppCacheGet).not.toHaveBeenCalled();
        });

        it("loads the SDK only after the viewer key exists", async () => {
            let resolveKey: (value: unknown) => void = () => undefined;
            mockMintMirisViewerKey.mockReturnValue(
                new Promise((resolve) => {
                    resolveKey = resolve;
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(1));
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            await act(async () => {
                resolveKey(staticKey());
            });
            await waitForScene();
            expect(mockLoadMirisStream).toHaveBeenCalledTimes(1);
        });

        it.each([
            ["a 502", [false, "bad gateway", 502, undefined]],
            ["a 504", [false, "timeout", 504, undefined]],
            ["a 500", [false, "boom", 500, undefined]],
            ["an unknown status", [false, "no response", undefined, undefined]],
            ["a different asset UUID", staticKey({ mirisAssetUuid: "other-uuid" })],
        ])("shows a retryable error on %s", async (_label, result) => {
            mockMintMirisViewerKey.mockResolvedValue(result);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/Could not get access to stream/i)).toBeInTheDocument()
            );
            expect(retryButton()).toBeInTheDocument();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
            expect(sceneElement()).toBeNull();
        });

        it("shows a permission error on a 403 that reports no state", async () => {
            mockMintMirisViewerKey.mockResolvedValue([false, "Forbidden", 403, undefined]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(
                    screen.getByText("You do not have permission to stream this asset.")
                ).toBeInTheDocument()
            );
            expect(sceneElement()).toBeNull();
        });

        it("shows the blocked request's message rather than a permission error", async () => {
            mockMintMirisViewerKey.mockResolvedValue([
                false,
                "Request blocked (request ID abc-123)",
                undefined,
                "requestBlocked",
            ]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText("Request blocked (request ID abc-123)")).toBeInTheDocument()
            );
            expect(
                screen.queryByText("You do not have permission to stream this asset.")
            ).not.toBeInTheDocument();
            expect(screen.queryByText(/Could not get access/i)).not.toBeInTheDocument();
            expect(sceneElement()).toBeNull();
        });

        it("shows the not-yet-streamed guidance when the key route reports no stream", async () => {
            mockMintMirisViewerKey.mockResolvedValue([false, "not streamed", 404, "notStreamed"]);
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() =>
                expect(screen.getByText(/Not yet streamed with Miris/i)).toBeInTheDocument()
            );
            expect(sceneElement()).toBeNull();
        });

        it("builds no scene when unmounted before the key arrives", async () => {
            let resolveKey: (value: unknown) => void = () => undefined;
            mockMintMirisViewerKey.mockReturnValue(
                new Promise((resolve) => {
                    resolveKey = resolve;
                })
            );
            const view = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(1));
            view.unmount();
            await act(async () => {
                resolveKey(staticKey());
            });
            expect(sceneElement()).toBeNull();
            expect(mockLoadMirisStream).not.toHaveBeenCalled();
        });

        it("keeps the scene and its key when only the viewed file version changes", async () => {
            const view = render(<MirisStreamViewerComponent {...baseProps} versionId="v1" />);
            const first = await waitForScene();

            view.rerender(<MirisStreamViewerComponent {...baseProps} versionId="v2" />);
            await waitFor(() => expect(screen.getByText(STALE_TEXT)).toBeInTheDocument());
            expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(1);
            expect(mockGetMirisStreamStatus).toHaveBeenCalledTimes(1);
            expect(sceneElement()).toBe(first);
        });
    });

    describe("per-view viewer keys", () => {
        const MINTED_KEY = "minted-key-abcdef";
        const mintOk = (overrides: Record<string, unknown> = {}) =>
            staticKey({ viewerKey: MINTED_KEY, expiresAt: "2026-10-15T13:00:00Z", ...overrides });

        beforeEach(() => {
            mockMintMirisViewerKey.mockResolvedValue(mintOk());
        });

        it("streams with the minted key", async () => {
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(sceneElement()).toHaveAttribute("viewer-key", MINTED_KEY);
            expect(streamElement()).toHaveAttribute("uuid", VALID_UUID);
        });

        it("never persists or logs the minted key", async () => {
            const localSet = jest.spyOn(Storage.prototype, "setItem");
            const logSpies = [
                jest.spyOn(console, "log").mockImplementation(() => undefined),
                jest.spyOn(console, "warn").mockImplementation(() => undefined),
                jest.spyOn(console, "error").mockImplementation(() => undefined),
            ];
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            expect(localSet).not.toHaveBeenCalled();
            expect(mockAppCacheSet).not.toHaveBeenCalled();
            for (const spy of logSpies) {
                expect(JSON.stringify(spy.mock.calls)).not.toContain(MINTED_KEY);
            }
            localSet.mockRestore();
            logSpies.forEach((spy) => spy.mockRestore());
        });

        it("mints again on a remount", async () => {
            const first = render(<MirisStreamViewerComponent {...baseProps} />);
            await waitForScene();
            first.unmount();
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(2));
            await waitForScene();
        });

        it("renders no scene until the minted key exists", async () => {
            let resolveMint: (value: unknown) => void = () => undefined;
            mockMintMirisViewerKey.mockReturnValue(
                new Promise((resolve) => {
                    resolveMint = resolve;
                })
            );
            render(<MirisStreamViewerComponent {...baseProps} />);
            await waitFor(() => expect(mockMintMirisViewerKey).toHaveBeenCalledTimes(1));
            expect(sceneElement()).toBeNull();
            await act(async () => {
                resolveMint(mintOk());
            });
            const scene = await waitForScene();
            expect(scene).toHaveAttribute("viewer-key", MINTED_KEY);
        });

        it("mounts a new scene element when the viewer key changes", async () => {
            mockMintMirisViewerKey
                .mockResolvedValueOnce(mintOk({ viewerKey: "first-key" }))
                .mockResolvedValueOnce(mintOk({ viewerKey: "second-key" }));
            render(<MirisStreamViewerComponent {...baseProps} />);
            const first = await waitForScene();
            expect(first).toHaveAttribute("viewer-key", "first-key");

            act(() => {
                first.dispatchEvent(new Event("error"));
            });
            act(() => {
                retryButton().click();
            });
            await waitFor(() => expect(sceneElement()).toHaveAttribute("viewer-key", "second-key"));
            expect(sceneElement()).not.toBe(first);
            expect(first.isConnected).toBe(false);
        });
    });
});
