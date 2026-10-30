/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

import { apiClient, isRequestBlocked } from "../apiClient";

/**
 * Miris stream recorded on one asset file, as returned by `GET /addon/miris/asset-status`.
 * `state` is `notStreamed`, `invalidUuid`, `notBound`, `unknown`, `processing`, or the Miris
 * processing state (`preview`, `streamable`, `error`, `failed`, ...).
 */
export interface MirisStreamStatus {
    filePath: string;
    mirisAssetUuid: string | null;
    sourceVersionId: string | null;
    uploadedAt: string | null;
    displayName: string | null;
    state: string;
}

/**
 * Miris viewer key for one asset file, as returned by `POST /addon/miris/viewer-key`. The key
 * streams `mirisAssetUuid`. `expiresAt` is when a per-view key stops authorizing new streams,
 * and null for the deployment's static viewer key.
 */
export interface MirisViewerKey {
    filePath: string;
    mirisAssetUuid: string;
    viewerKey: string;
    expiresAt: string | null;
}

/** `state` reported for a request the deployment's AWS WAF web ACL blocked. */
export const MIRIS_REQUEST_BLOCKED = "requestBlocked";

/**
 * Failure tuple for a request that threw: the message, the HTTP status when the request reached
 * the API, and the error body's `state`. A WAF-blocked request carries no status, so it is not
 * read as a permission denial.
 */
function requestFailure(
    error: any,
    fallbackMessage: string
): [false, string, number | undefined, string | undefined] {
    const message = error?.message || fallbackMessage;
    if (isRequestBlocked(error)) {
        return [false, message, undefined, MIRIS_REQUEST_BLOCKED];
    }
    return [
        false,
        message,
        typeof error?.status === "number" ? error.status : undefined,
        typeof error?.body?.state === "string" ? error.body.state : undefined,
    ];
}

interface MirisFileRequest {
    databaseId: string;
    assetId: string;
    filePath: string;
}

/**
 * Fetch the Miris stream status of a file. The server reads the Miris asset UUID from the
 * file's metadata, so the caller supplies only the asset-relative file path or object key.
 *
 * Returns [true, MirisStreamStatus] on success, or [false, errorMessage, status, state] on
 * failure, where `status` is the HTTP status when the request reached the API (403 for a
 * permission denial) and undefined otherwise. A request the AWS WAF web ACL blocked reports an
 * undefined status and the state `requestBlocked`.
 */
export const getMirisStreamStatus = async ({
    databaseId,
    assetId,
    filePath,
}: MirisFileRequest): Promise<
    [true, MirisStreamStatus] | [false, string, number | undefined, string | undefined]
> => {
    try {
        const response = await apiClient.get("addon/miris/asset-status", {
            queryStringParameters: { databaseId, assetId, filePath },
        });
        if (response && typeof response === "object" && typeof response.state === "string") {
            return [true, response as MirisStreamStatus];
        }
        return [false, response?.message || "Unknown response shape", undefined, undefined];
    } catch (error: any) {
        console.log(error);
        return requestFailure(error, "Failed to fetch Miris stream status");
    }
};

/**
 * Get a viewer key for a file: the deployment's viewer key in static mode, or a key minted for
 * this one Miris asset in per-view mode. The server reads the Miris asset UUID from the file's
 * metadata, so the caller supplies only the file path. The key is returned to the caller only;
 * do not cache or persist it.
 *
 * Returns [true, MirisViewerKey] on success, or [false, errorMessage, status, state] on failure,
 * where `status` is the HTTP status when the request reached the API (403 for a permission denial
 * or an unbound Miris asset, 404 when the file records no stream, 502 when Miris could not be
 * read or did not issue a key, 503 when the add-on is not configured, 504 when Miris did not
 * answer in time) and undefined otherwise, and `state` is the error body's `state` (`notBound`
 * when the Miris asset lacks the `vams-asset-{assetId}` or `vams-database-{databaseId}` tag), when
 * it reports one. A request the AWS WAF web ACL blocked is not a permission denial: it reports an
 * undefined status and the state `requestBlocked`.
 */
export const mintMirisViewerKey = async ({
    databaseId,
    assetId,
    filePath,
}: MirisFileRequest): Promise<
    [true, MirisViewerKey] | [false, string, number | undefined, string | undefined]
> => {
    try {
        const response = await apiClient.post("addon/miris/viewer-key", {
            body: { databaseId, assetId, filePath },
        });
        if (
            response &&
            typeof response === "object" &&
            typeof response.viewerKey === "string" &&
            response.viewerKey !== "" &&
            typeof response.mirisAssetUuid === "string"
        ) {
            return [true, response as MirisViewerKey];
        }
        return [false, "Unknown response shape", undefined, undefined];
    } catch (error: any) {
        console.log(error?.message);
        return requestFailure(error, "Failed to get a Miris viewer key");
    }
};
