/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

import { getMirisStreamStatus, MIRIS_REQUEST_BLOCKED, mintMirisViewerKey } from "./mirisAPIService";
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
