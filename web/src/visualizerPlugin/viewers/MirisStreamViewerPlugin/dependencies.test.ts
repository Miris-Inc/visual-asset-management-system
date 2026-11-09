/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
import fs from "fs";
import path from "path";
import { waitFor } from "@testing-library/react";
import type * as Dependencies from "./dependencies";

// jsdom cannot evaluate the SDK, so the bundle the install publishes at MIRIS_SDK_URL is a
// stand-in. Importing the real bundle evaluates `@miris-inc/core` and `@miris-inc/three`, whose
// top-level await requests the WebGPU adapter, and its `useWebGPU()` is
// `adapter !== null && Miris.tryUseShark`; the stand-in takes the adapter the test grants. The real
// `defineElements()` evaluates `@miris-inc/components`, which registers the custom elements, so the
// stand-in records the renderer choice at that moment.
const mockMiris = { tryUseShark: true };
const mockGpu: { adapter: object | null } = { adapter: null };
const mockEvents: string[] = [];
let mockComponentsFail = false;
// Each `defineElements()` takes the next gate, if any, and settles with it.
const mockComponentsGates: Array<Promise<void>> = [];
let mockComponentsImports = 0;

jest.mock(
    "/viewers/miris/miris-sdk.js",
    () => {
        mockEvents.push("sdk");
        const canUseShark = mockGpu.adapter !== null;
        return {
            Miris: mockMiris,
            useWebGPU: () => canUseShark && mockMiris.tryUseShark,
            defineElements: () => {
                mockComponentsImports += 1;
                if (mockComponentsFail) return Promise.reject(new Error("chunk failed"));
                mockEvents.push(`components tryUseShark=${mockMiris.tryUseShark}`);
                return mockComponentsGates.shift() ?? Promise.resolve();
            },
        };
    },
    { virtual: true }
);

// The manager keeps its loaded/in-flight state in statics, and the SDK stand-in records its own
// evaluation, so each test takes fresh module instances of both.
const freshModule = () => {
    jest.resetModules();
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    return require("./dependencies") as typeof Dependencies;
};

// "absent": no navigator.gpu. "noAdapter": navigator.gpu exists but grants no adapter, as on a
// blocklisted GPU, with hardware acceleration off, or in a VDI session.
const setGpu = (state: "absent" | "noAdapter" | "adapter") => {
    if (state === "absent") {
        delete (navigator as Navigator & { gpu?: unknown }).gpu;
        mockGpu.adapter = null;
        return;
    }
    const adapter = state === "adapter" ? {} : null;
    Object.defineProperty(navigator, "gpu", {
        value: { requestAdapter: () => Promise.resolve(adapter) },
        configurable: true,
    });
    mockGpu.adapter = adapter;
};

const mockLoseContext = jest.fn();
const getContext = jest.spyOn(HTMLCanvasElement.prototype, "getContext");
const setWebGl2 = (available: boolean) => {
    getContext.mockImplementation(((type: string) =>
        available && type === "webgl2"
            ? {
                  getExtension: (name: string) =>
                      name === "WEBGL_lose_context" ? { loseContext: mockLoseContext } : null,
              }
            : null) as unknown as typeof HTMLCanvasElement.prototype.getContext);
};

beforeEach(() => {
    mockMiris.tryUseShark = true;
    mockEvents.length = 0;
    mockComponentsFail = false;
    mockLoseContext.mockClear();
    getContext.mockClear();
    setGpu("absent");
    setWebGl2(true);
});

afterAll(() => {
    setGpu("absent");
    getContext.mockRestore();
});

describe("hasWebGl2", () => {
    it("reports a WebGL 2 context and releases it", () => {
        const { hasWebGl2 } = freshModule();
        expect(hasWebGl2()).toBe(true);
        expect(getContext).toHaveBeenCalledWith("webgl2");
        expect(mockLoseContext).toHaveBeenCalledTimes(1);
    });

    it("reports false when the canvas yields no WebGL 2 context", () => {
        setWebGl2(false);
        const { hasWebGl2 } = freshModule();
        expect(hasWebGl2()).toBe(false);
    });

    it("reports false when creating the context throws", () => {
        getContext.mockImplementation(() => {
            throw new Error("context creation failed");
        });
        const { hasWebGl2 } = freshModule();
        expect(hasWebGl2()).toBe(false);
    });
});

describe("MirisStreamDependencyManager.loadMirisStream", () => {
    it("selects the WebGL engine before the elements load when WebGPU is absent", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(mockMiris.tryUseShark).toBe(false);
        expect(mockEvents).toEqual(["sdk", "components tryUseShark=false"]);
    });

    it("selects the WebGL engine when navigator.gpu grants no adapter", async () => {
        setGpu("noAdapter");
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(mockMiris.tryUseShark).toBe(false);
        expect(mockEvents).toEqual(["sdk", "components tryUseShark=false"]);
    });

    it("leaves the SDK's WebGPU default in place when an adapter is granted", async () => {
        setGpu("adapter");
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(mockMiris.tryUseShark).toBe(true);
        expect(mockEvents).toEqual(["sdk", "components tryUseShark=true"]);
    });

    it("supports the WebGL engine when WebGL 2 is available", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(getContext).toHaveBeenCalledTimes(1);
        expect(MirisStreamDependencyManager.isDeviceSupported()).toBe(true);
    });

    it("reports the device unsupported on the WebGL engine without WebGL 2", async () => {
        setGpu("noAdapter");
        setWebGl2(false);
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(MirisStreamDependencyManager.isDeviceSupported()).toBe(false);
    });

    it("does not probe WebGL 2 on the WebGPU engine, which never creates a WebGL context", async () => {
        setGpu("adapter");
        setWebGl2(false);
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        expect(getContext).not.toHaveBeenCalled();
        expect(MirisStreamDependencyManager.isDeviceSupported()).toBe(true);
    });

    it("shares one load between concurrent callers and is a no-op once loaded", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        await Promise.all([
            MirisStreamDependencyManager.loadMirisStream(),
            MirisStreamDependencyManager.loadMirisStream(),
        ]);
        await MirisStreamDependencyManager.loadMirisStream();
        expect(mockEvents).toEqual(["sdk", "components tryUseShark=false"]);
        expect(getContext).toHaveBeenCalledTimes(1);
    });

    it("starts a new attempt after the pending load is abandoned, and a stale attempt does not clear it", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        let failA: (reason: Error) => void = () => undefined;
        let openB: () => void = () => undefined;
        mockComponentsGates.push(
            new Promise<void>((_resolve, reject) => (failA = reject)),
            new Promise<void>((resolve) => (openB = resolve))
        );
        mockComponentsImports = 0;

        const first = MirisStreamDependencyManager.loadMirisStream();
        MirisStreamDependencyManager.abandonPendingLoad();
        const second = MirisStreamDependencyManager.loadMirisStream();
        await waitFor(() => expect(mockComponentsImports).toBe(2));

        // The abandoned attempt fails, so it never marks the SDK loaded, and runs its finally
        // while the second is still pending.
        failA(new Error("stale attempt failed"));
        await expect(first).rejects.toThrow("stale attempt failed");
        const importsBefore = mockComponentsImports;
        const third = MirisStreamDependencyManager.loadMirisStream();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(mockComponentsImports).toBe(importsBefore);

        openB();
        await expect(Promise.all([second, third])).resolves.toBeDefined();
        expect(getContext).toHaveBeenCalledTimes(2);
    });

    it("rejects when the elements fail to load and retries on the next call", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        mockComponentsFail = true;
        await expect(MirisStreamDependencyManager.loadMirisStream()).rejects.toThrow(
            "chunk failed"
        );

        mockComponentsFail = false;
        jest.resetModules();
        await expect(MirisStreamDependencyManager.loadMirisStream()).resolves.toBeUndefined();
        expect(mockEvents).toContain("components tryUseShark=false");
    });
});

describe("MirisStreamDependencyManager.cleanup", () => {
    it("allows a later load", async () => {
        const { MirisStreamDependencyManager } = freshModule();
        await MirisStreamDependencyManager.loadMirisStream();
        MirisStreamDependencyManager.cleanup();

        // The modules are cached, so a reload re-applies the renderer choice without re-evaluating.
        mockMiris.tryUseShark = true;
        await MirisStreamDependencyManager.loadMirisStream();
        expect(mockMiris.tryUseShark).toBe(false);
    });
});

describe("MIRIS_SDK_URL", () => {
    it("is where the custom install publishes the bundle's entry", () => {
        // public/ is served from the site root, so the install's destination and the webpack entry
        // file name together give the URL the viewer imports.
        const installDir = path.join(__dirname, "../../../../customInstalls/miris");
        const install = fs.readFileSync(path.join(installDir, "mirisInstall.js"), "utf8");
        const webpackConfig = jest.requireActual(path.join(installDir, "webpack.config.js"));
        const destination = /const destinationDir = "\.\/public(\/[^"]+)";/.exec(install)?.[1];
        const entry = /const bundleEntryFile = "([^"]+)";/.exec(install)?.[1];

        const { MIRIS_SDK_URL } = freshModule();
        expect(`${destination}/${entry}`).toBe(MIRIS_SDK_URL);
        expect(webpackConfig.output.filename).toBe(entry);
    });
});
