/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

import {
    findRunningMirisUpload,
    getMirisStreamStatus,
    isExecutionInProgress,
    MIRIS_REQUEST_BLOCKED,
    mintMirisViewerKey,
    startMirisUpload,
} from "./mirisAPIService";
import { ApiError, apiClient } from "../apiClient";

jest.mock("../apiClient", () => {
    const actual = jest.requireActual("../apiClient");
    return {
        ApiError: actual.ApiError,
        isRequestBlocked: actual.isRequestBlocked,
        apiClient: { get: jest.fn(), post: jest.fn() },
    };
});

const mockGet = apiClient.get as jest.Mock;
const mockPost = apiClient.post as jest.Mock;

const ARGS = { databaseId: "db-1", assetId: "asset-1", filePath: "/folder/scene.usd" };

const STATUS = {
    filePath: "/folder/scene.usd",
    mirisAssetUuid: "8d65af7c-5412-404b-a494-fbd2cdb62442",
    sourceVersionId: "v-abc",
    uploadedAt: "2026-10-14T12:00:00Z",
    displayName: "Scene",
    state: "streamable",
};

const wafBlocked = () =>
    new ApiError("Request blocked (request ID abc-123)", 403, {
        message: "Request blocked",
        blockedBy: "waf",
        requestId: "abc-123",
    });

beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, "log").mockImplementation(() => undefined);
});

afterEach(() => {
    jest.restoreAllMocks();
});

describe("getMirisStreamStatus", () => {
    it("calls the add-on status route with the file named in the query string", async () => {
        mockGet.mockResolvedValue(STATUS);
        await getMirisStreamStatus(ARGS);
        expect(mockGet).toHaveBeenCalledWith("addon/miris/asset-status", {
            queryStringParameters: {
                databaseId: "db-1",
                assetId: "asset-1",
                filePath: "/folder/scene.usd",
            },
        });
    });

    it("returns the status on success", async () => {
        mockGet.mockResolvedValue(STATUS);
        expect(await getMirisStreamStatus(ARGS)).toEqual([true, STATUS]);
    });

    it("returns notStreamed as a successful status", async () => {
        const notStreamed = {
            ...STATUS,
            mirisAssetUuid: null,
            sourceVersionId: null,
            uploadedAt: null,
            displayName: null,
            state: "notStreamed",
        };
        mockGet.mockResolvedValue(notStreamed);
        expect(await getMirisStreamStatus(ARGS)).toEqual([true, notStreamed]);
    });

    it.each([400, 403, 500])(
        "returns the message and HTTP status %i on failure",
        async (status) => {
            mockGet.mockRejectedValue(new ApiError(`failed ${status}`, status));
            expect(await getMirisStreamStatus(ARGS)).toEqual([
                false,
                `failed ${status}`,
                status,
                undefined,
            ]);
        }
    );

    it("reports a request the AWS WAF web ACL blocked apart from a permission denial", async () => {
        mockGet.mockRejectedValue(wafBlocked());
        expect(await getMirisStreamStatus(ARGS)).toEqual([
            false,
            "Request blocked (request ID abc-123)",
            undefined,
            MIRIS_REQUEST_BLOCKED,
        ]);
    });

    it("returns an undefined status when the request did not reach the API", async () => {
        mockGet.mockRejectedValue(new TypeError("Failed to fetch"));
        expect(await getMirisStreamStatus(ARGS)).toEqual([
            false,
            "Failed to fetch",
            undefined,
            undefined,
        ]);
    });

    it("returns an error for a response without a state", async () => {
        mockGet.mockResolvedValue({ message: "Validation Error" });
        expect(await getMirisStreamStatus(ARGS)).toEqual([
            false,
            "Validation Error",
            undefined,
            undefined,
        ]);
    });

    it("returns a default message for an empty response", async () => {
        mockGet.mockResolvedValue(undefined);
        expect(await getMirisStreamStatus(ARGS)).toEqual([
            false,
            "Unknown response shape",
            undefined,
            undefined,
        ]);
    });
});

const KEY = {
    filePath: "/folder/scene.usd",
    mirisAssetUuid: "8d65af7c-5412-404b-a494-fbd2cdb62442",
    viewerKey: "minted-viewer-key",
    expiresAt: "2026-10-15T19:04:28Z",
};

describe("mintMirisViewerKey", () => {
    it("posts the asset and file to the add-on viewer key route", async () => {
        mockPost.mockResolvedValue(KEY);
        await mintMirisViewerKey(ARGS);
        expect(mockPost).toHaveBeenCalledWith("addon/miris/viewer-key", {
            body: { databaseId: "db-1", assetId: "asset-1", filePath: "/folder/scene.usd" },
        });
    });

    it("returns a per-view key on success", async () => {
        mockPost.mockResolvedValue(KEY);
        expect(await mintMirisViewerKey(ARGS)).toEqual([true, KEY]);
    });

    it("returns the static key, which has no expiry, on success", async () => {
        const staticKey = { ...KEY, viewerKey: "deployment-viewer-key", expiresAt: null };
        mockPost.mockResolvedValue(staticKey);
        expect(await mintMirisViewerKey(ARGS)).toEqual([true, staticKey]);
    });

    it.each([403, 404, 500, 502, 503, 504])(
        "returns the HTTP status %i on failure",
        async (status) => {
            mockPost.mockRejectedValue(new ApiError(`failed ${status}`, status));
            const result = await mintMirisViewerKey(ARGS);
            expect(result).toHaveLength(4);
            expect(result).toEqual([false, `failed ${status}`, status, undefined]);
        }
    );

    it("returns the state the error body reports", async () => {
        mockPost.mockRejectedValue(
            new ApiError("not linked", 403, { state: "notBound", message: "not linked" })
        );
        expect(await mintMirisViewerKey(ARGS)).toEqual([false, "not linked", 403, "notBound"]);
    });

    it("ignores a non-string state in the error body", async () => {
        mockPost.mockRejectedValue(new ApiError("Forbidden", 403, { state: 7 }));
        expect(await mintMirisViewerKey(ARGS)).toEqual([false, "Forbidden", 403, undefined]);
    });

    it("reports a request the AWS WAF web ACL blocked apart from a permission denial", async () => {
        mockPost.mockRejectedValue(wafBlocked());
        expect(await mintMirisViewerKey(ARGS)).toEqual([
            false,
            "Request blocked (request ID abc-123)",
            undefined,
            MIRIS_REQUEST_BLOCKED,
        ]);
    });

    it("[control] a 403 without the WAF marker is still a permission denial", async () => {
        mockPost.mockRejectedValue(new ApiError("Forbidden", 403, { blockedBy: "other" }));
        expect(await mintMirisViewerKey(ARGS)).toEqual([false, "Forbidden", 403, undefined]);
    });

    it("returns an undefined status when the request did not reach the API", async () => {
        mockPost.mockRejectedValue(new TypeError("Failed to fetch"));
        expect(await mintMirisViewerKey(ARGS)).toEqual([
            false,
            "Failed to fetch",
            undefined,
            undefined,
        ]);
    });

    it.each([
        ["an empty viewer key", { ...KEY, viewerKey: "" }],
        ["no asset UUID", { ...KEY, mirisAssetUuid: undefined }],
        ["no body", undefined],
    ])("treats a response with %s as a failure", async (_label, response) => {
        mockPost.mockResolvedValue(response);
        expect(await mintMirisViewerKey(ARGS)).toEqual([
            false,
            "Unknown response shape",
            undefined,
            undefined,
        ]);
    });

    it("never logs the key material", async () => {
        mockPost.mockResolvedValue(KEY);
        await mintMirisViewerKey(ARGS);
        expect(JSON.stringify((console.log as jest.Mock).mock.calls)).not.toContain(KEY.viewerKey);
    });
});

describe("startMirisUpload", () => {
    it("executes the GLOBAL miris-upload workflow on the one file, as a manual run", async () => {
        mockPost.mockResolvedValue({ message: { executionId: "exec-1" } });
        await startMirisUpload(ARGS);
        expect(mockPost).toHaveBeenCalledWith("workflows/GLOBAL/miris-upload/execute", {
            body: {
                inputFiles: [
                    {
                        databaseId: "db-1",
                        assetId: "asset-1",
                        relativeFileKey: "/folder/scene.usd",
                    },
                ],
                triggerType: "manual",
            },
        });
    });

    it("names no template, so the pipeline's default template applies", async () => {
        mockPost.mockResolvedValue({ message: { executionId: "exec-1" } });
        await startMirisUpload(ARGS);
        expect(mockPost.mock.calls[0][1].body.pipelineExecutionParameters).toBeUndefined();
    });

    it("returns the unwrapped execute response on success", async () => {
        mockPost.mockResolvedValue({
            message: { executionId: "exec-1", warnings: ["note"] },
        });
        expect(await startMirisUpload(ARGS)).toEqual([
            true,
            { executionId: "exec-1", warnings: ["note"] },
        ]);
    });

    it.each([400, 403, 500])(
        "returns the message and HTTP status %i on failure",
        async (statusCode) => {
            mockPost.mockRejectedValue(new ApiError("denied", statusCode, { message: "denied" }));
            expect(await startMirisUpload(ARGS)).toEqual([false, "denied", statusCode, undefined]);
        }
    );

    it("reports a WAF-blocked request as blocked rather than as a 403", async () => {
        mockPost.mockRejectedValue(wafBlocked());
        expect(await startMirisUpload(ARGS)).toEqual([
            false,
            "Request blocked (request ID abc-123)",
            undefined,
            MIRIS_REQUEST_BLOCKED,
        ]);
    });
});

describe("findRunningMirisUpload", () => {
    const ASSET = { databaseId: "db-1", assetId: "asset-1" };
    const PATH = "database/db-1/assets/asset-1/workflows/executions";
    const execution = (overrides: Record<string, unknown> = {}) => ({
        workflowExecutionId: "exec-1",
        workflowId: "miris-upload",
        workflowDatabaseId: "GLOBAL",
        executionStatus: "RUNNING",
        executionStartDate: "2026-10-29T10:00:00Z",
        executionStopDate: "",
        ...overrides,
    });
    const page = (items: unknown[], nextToken?: string) => ({
        message: {
            Items: items,
            filterStartDate: "2026-07-31T10:00:00Z",
            ...(nextToken ? { NextToken: nextToken } : {}),
        },
    });

    it("lists the asset's executions filtered to the GLOBAL miris-upload workflow", async () => {
        mockGet.mockResolvedValue(page([]));
        await findRunningMirisUpload(ASSET);
        expect(mockGet).toHaveBeenCalledTimes(1);
        expect(mockGet).toHaveBeenCalledWith(PATH, {
            queryStringParameters: { workflowDatabaseId: "GLOBAL", workflowId: "miris-upload" },
        });
    });

    it.each(["RUNNING", "NEW"])("returns a %s run", async (executionStatus) => {
        const run = execution({ executionStatus });
        mockGet.mockResolvedValue(page([run]));
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, run]);
    });

    it("returns null when every run has finished", async () => {
        mockGet.mockResolvedValue(
            page([
                execution({ workflowExecutionId: "a", executionStatus: "SUCCEEDED" }),
                execution({ workflowExecutionId: "b", executionStatus: "FAILED" }),
                execution({ workflowExecutionId: "c", executionStatus: "ABORTED" }),
                execution({ workflowExecutionId: "d", executionStatus: "TIMED_OUT" }),
                execution({
                    workflowExecutionId: "e",
                    executionStopDate: "2026-10-29T11:00:00Z",
                }),
            ])
        );
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, null]);
    });

    it("ignores a run of another workflow", async () => {
        mockGet.mockResolvedValue(page([execution({ workflowId: "other" })]));
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, null]);
    });

    it("follows NextToken until it finds a run", async () => {
        const run = execution({ workflowExecutionId: "exec-2" });
        mockGet
            .mockResolvedValueOnce(page([], "token-1"))
            .mockResolvedValueOnce(page([run], "token-2"));
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, run]);
        expect(mockGet).toHaveBeenCalledTimes(2);
        expect(mockGet).toHaveBeenLastCalledWith(PATH, {
            queryStringParameters: {
                workflowDatabaseId: "GLOBAL",
                workflowId: "miris-upload",
                startingToken: "token-1",
            },
        });
    });

    it("stops on a repeated NextToken", async () => {
        mockGet.mockResolvedValue(page([], "token-1"));
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, null]);
        expect(mockGet).toHaveBeenCalledTimes(2);
    });

    it("stops after its page bound", async () => {
        let n = 0;
        mockGet.mockImplementation(async () => page([], `token-${++n}`));
        expect(await findRunningMirisUpload(ASSET)).toEqual([true, null]);
        expect(mockGet).toHaveBeenCalledTimes(10);
    });

    it("reports a failed request", async () => {
        mockGet.mockRejectedValue(new ApiError("Forbidden", 403, { message: "Forbidden" }));
        expect(await findRunningMirisUpload(ASSET)).toEqual([false, "Forbidden"]);
    });

    it("reports an unknown response shape", async () => {
        mockGet.mockResolvedValue({ message: "nope" });
        expect(await findRunningMirisUpload(ASSET)).toEqual([false, "Unknown response shape"]);
    });
});

describe("isExecutionInProgress", () => {
    it.each([
        [{ executionStatus: "RUNNING" }, true],
        [{ executionStatus: "NEW" }, true],
        [{ executionStatus: "SUCCEEDED" }, false],
        [{ executionStatus: "COMPLETE" }, false],
        [{ executionStatus: "RUNNING", executionStopDate: "2026-10-29T11:00:00Z" }, false],
    ])("%o -> %s", (execution, expected) => {
        expect(isExecutionInProgress(execution as any)).toBe(expected);
    });
});
