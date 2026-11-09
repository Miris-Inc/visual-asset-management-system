/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The SDK bundle that `web/customInstalls/miris/mirisInstall.js` builds and publishes under
 * `public/viewers/miris/` when this viewer is enabled. It is an ES module whose chunks and `.wasm`
 * files sit beside it, and it is imported at runtime rather than bundled into the web app.
 */
export const MIRIS_SDK_URL = "/viewers/miris/miris-sdk.js";

/** The exports of the bundle's entry, `web/customInstalls/miris/mirisSdkEntry.js`. */
export interface MirisSdkModule {
    /** `Miris` from `@miris-inc/core`. */
    Miris: { tryUseShark: boolean };
    /** `useWebGPU` from `@miris-inc/three`, settled once the bundle has evaluated. */
    useWebGPU: () => boolean;
    /** Evaluates `@miris-inc/components`, which registers the custom elements. */
    defineElements: () => Promise<unknown>;
}

/**
 * True when a throwaway canvas yields a WebGL 2 context, which the SDK's WebGL engine requires.
 * The context is released at once so the probe does not count against the browser's limit.
 */
export function hasWebGl2(): boolean {
    if (typeof document === "undefined") return false;
    try {
        const gl = document.createElement("canvas").getContext("webgl2");
        if (!gl) return false;
        gl.getExtension("WEBGL_lose_context")?.loseContext();
        return true;
    } catch {
        return false;
    }
}

export class MirisStreamDependencyManager {
    private static loaded = false;
    private static pending: Promise<void> | null = null;
    private static supported = true;

    /**
     * Registers the Miris custom elements (<miris-scene>, <miris-camera>, <miris-stream>,
     * <miris-group>, <miris-grid>). The SDK renders with WebGPU by default and shows a "needs
     * WebGPU" notice when the browser grants no WebGPU adapter, so without one the WebGL engine
     * is selected first, by the SDK's own `useWebGPU()` (`@miris-inc/three` requests the adapter
     * in a top-level await, so the answer is settled once the bundle's import resolves).
     * `Miris.tryUseShark` is read when a scene is created, so it is set before any element can
     * exist, on the same `@miris-inc/core` module the elements import (one bundle, exact pins).
     */
    static async loadMirisStream(): Promise<void> {
        if (MirisStreamDependencyManager.loaded) return;
        const attempt = (MirisStreamDependencyManager.pending ??= (async () => {
            const sdk: MirisSdkModule = await import(/* @vite-ignore */ MIRIS_SDK_URL);
            const webGpu = sdk.useWebGPU();
            if (!webGpu) sdk.Miris.tryUseShark = false;
            MirisStreamDependencyManager.supported = webGpu || hasWebGl2();
            await sdk.defineElements();
            MirisStreamDependencyManager.loaded = true;
        })().finally(() => {
            if (MirisStreamDependencyManager.pending === attempt) {
                MirisStreamDependencyManager.pending = null;
            }
        }));
        await attempt;
    }

    /**
     * Drops the in-flight load so the next `loadMirisStream()` starts a new attempt instead of
     * joining one that never settled. The abandoned attempt still marks the SDK loaded if it
     * eventually completes.
     */
    static abandonPendingLoad(): void {
        MirisStreamDependencyManager.pending = null;
    }

    /**
     * Whether the engine chosen by the last load can render here. The WebGL engine needs a
     * WebGL 2 context: a scene mounted without one rejects while connecting and fires no event.
     * A WebGL 2 browser without WebGPU counts as supported, although some assets (GES) need
     * WebGPU and the SDK shows its own notice for those.
     */
    static isDeviceSupported(): boolean {
        return MirisStreamDependencyManager.supported;
    }

    /**
     * Lets the next open repeat the renderer choice and device check. Custom elements cannot be
     * unregistered, and the browser keeps the SDK modules cached in case the user switches back.
     */
    static cleanup(): void {
        MirisStreamDependencyManager.loaded = false;
    }
}
