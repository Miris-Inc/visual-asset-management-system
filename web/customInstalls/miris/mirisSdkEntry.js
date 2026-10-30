/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

// Webpack entry for the Miris Spatial Streaming SDK bundle (webpack.config.js), served from
// /viewers/miris/ and imported at runtime by MirisStreamViewerPlugin/dependencies.ts.
//
// Evaluating this module evaluates @miris-inc/core and @miris-inc/three, whose top-level await
// requests the WebGPU adapter, so `useWebGPU()` is settled once the import resolves. The custom
// elements (<miris-scene>, <miris-camera>, <miris-stream>, <miris-group>, <miris-grid>) register
// when @miris-inc/components evaluates, which `defineElements()` defers to a separate chunk so the
// viewer can select the renderer on `Miris` first. Both chunks share one @miris-inc/core and one
// three instance.

export { Miris } from "@miris-inc/core";
export { useWebGPU } from "@miris-inc/three";

export const defineElements = () =>
    import(/* webpackChunkName: "miris-components" */ "@miris-inc/components");
