/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
import {
    isStaleStream,
    latestVersionIdFromFileInfo,
    streamDisposition,
    toStatusFilePath,
} from "./streamStatus";

describe("streamDisposition", () => {
    it.each([
        ["notStreamed", "notStreamed"],
        ["invalidUuid", "invalidUuid"],
        ["notBound", "notBound"],
        ["error", "failed"],
        ["failed", "failed"],
        ["streamable", "stream"],
        ["unknown", "stream"],
        ["preview", "processing"],
    ])("maps %s to %s", (state, disposition) => {
        expect(streamDisposition(state)).toBe(disposition);
    });
});

describe("toStatusFilePath", () => {
    it.each([
        ["asset-1/folder/scene.usd", "/asset-1/folder/scene.usd"],
        ["/asset-1/folder/scene.usd", "/asset-1/folder/scene.usd"],
        ["assets/asset-1/scene.usd", "/assets/asset-1/scene.usd"],
        ["projects/site-a/sub/scene.usd", "/projects/site-a/sub/scene.usd"],
        ["//scene.usd", "/scene.usd"],
    ])("maps %s to %s", (assetKey, expected) => {
        expect(toStatusFilePath(assetKey)).toBe(expected);
    });
});

describe("isStaleStream", () => {
    it("is stale when both versions are known and differ", () => {
        expect(isStaleStream("v1", "v2")).toBe(true);
    });

    it("is not stale when the versions match", () => {
        expect(isStaleStream("v1", "v1")).toBe(false);
    });

    it.each([[""], [null], [undefined], ["null"]])(
        "is not stale for an unrecorded source version %p",
        (sourceVersionId) => {
            expect(isStaleStream(sourceVersionId, "v2")).toBe(false);
        }
    );

    it.each([[""], [null], [undefined], ["null"]])(
        "is not stale for an unknown viewed version %p",
        (viewedVersionId) => {
            expect(isStaleStream("v1", viewedVersionId)).toBe(false);
        }
    );
});

describe("latestVersionIdFromFileInfo", () => {
    it("returns the version flagged isLatest", () => {
        expect(
            latestVersionIdFromFileInfo({
                versions: [
                    { versionId: "v1", isLatest: false },
                    { versionId: "v2", isLatest: true },
                ],
            })
        ).toBe("v2");
    });

    it("falls back to the first listed version", () => {
        expect(latestVersionIdFromFileInfo({ versions: [{ versionId: "v3" }] })).toBe("v3");
    });

    it("parses a JSON string response", () => {
        expect(
            latestVersionIdFromFileInfo(
                JSON.stringify({ versions: [{ versionId: "v4", isLatest: true }] })
            )
        ).toBe("v4");
    });

    it.each([[undefined], ["not json"], [{}], [{ versions: [{ versionId: "null" }] }]])(
        "returns undefined for %p",
        (fileInfo) => {
            expect(latestVersionIdFromFileInfo(fileInfo)).toBeUndefined();
        }
    );
});
