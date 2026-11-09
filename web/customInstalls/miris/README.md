# Miris Spatial Streaming SDK Bundle Installation

## Overview

This directory builds the Miris web SDK for the Miris Spatial Streaming viewer (`miris-stream-viewer`, part of the Miris Spatial Streaming add-on). The SDK is not a dependency of the web application: like the VNTANA and VEERUM viewers, it is installed and bundled here, and only when the viewer is enabled.

## Enabling the viewer

1. Open `web/src/visualizerPlugin/config/addonViewerConfig.json` (the add-on viewer configuration).
2. Find the `miris-stream-viewer` entry.
3. Set `"enabled": true`.
4. Run `npm install` (or `npm ci`) in `web/`.

The viewer is offered only when the deployment also enables the add-on (`app.addons.useMirisStreaming`, which sets the `MIRIS_ADDON` feature switch) and `app.webUi.allowUnsafeEvalFeatures` (`ALLOWUNSAFEEVAL`). See `documentation/docusaurus-site/docs/developer/miris-spatial-streaming.md`.

## Files

-   **`package.json`** / **`package-lock.json`**: the SDK packages, pinned exactly, and webpack.
    -   `@miris-inc/components`, `@miris-inc/core` and `@miris-inc/three` share one version: they publish in lockstep and pin one another exactly.
    -   `three` is pinned inside the peer range the SDK packages declare (`>=0.182.0 <0.186.0` for the pinned SDK).
-   **`mirisSdkEntry.js`**: the bundle entry. It exports `Miris` (`@miris-inc/core`), `useWebGPU` (`@miris-inc/three`) and `defineElements()`, which evaluates `@miris-inc/components` from a separate chunk.
-   **`webpack.config.js`**: bundles the entry into an ES module, `dist/miris-sdk.js`, with its chunks.
-   **`mirisInstall.js`**: the install script, run by the `postinstall` chain in `web/package.json`.

## Installation process

`mirisInstall.js`:

1. Removes the previous `node_modules/`, `dist/` and `web/public/viewers/miris/`.
2. Stops when `checkViewerEnabled("miris-stream-viewer")` (`web/customInstalls/utility/checkViewerEnabled.js`) reports the viewer disabled.
3. Runs `npm ci` here.
4. Runs webpack (`npm run build`).
5. Copies `dist/` and the `.wasm` files of `@miris-inc/core` and `@miris-inc/three` to `web/public/viewers/miris/`, which the web build copies to `viewers/miris/`.
6. Fails if a chunk references a `.wasm` file that was not copied.

To rebuild only this bundle, run from `web/`:

```bash
node customInstalls/miris/mirisInstall.js
```

## Bundle format

The SDK packages are ES modules with a top-level await (`@miris-inc/three` requests the WebGPU adapter while it evaluates), so the bundle is an ES module rather than a UMD script, and the viewer loads it with a dynamic `import("/viewers/miris/miris-sdk.js")` (`web/src/visualizerPlugin/viewers/MirisStreamViewerPlugin/dependencies.ts`).

The SDK locates its WebAssembly with `new URL("<name>.wasm", import.meta.url)`. The webpack configuration leaves `import.meta` to the browser, so each chunk resolves its `.wasm` files against its own URL, and the install script serves them beside the chunks. The chunks import one another by relative URL.

The bundle is same-origin, so it needs no Content Security Policy source beyond `'self'`. The add-on's own sources (`*.miris.com` in `connect-src`, `blob:` in `script-src`) and `'unsafe-eval'` from `app.webUi.allowUnsafeEvalFeatures` cover what the SDK does at runtime.

## Version updates

1. Update the three `@miris-inc/*` versions together, and `three` within the SDK's peer range (`npm view @miris-inc/three@<version> peerDependencies`), in `package.json`.
2. Run `npm install` in this directory to update `package-lock.json`.
3. Run `node customInstalls/miris/mirisInstall.js` from `web/` with the viewer enabled, and test the viewer.
4. Update the versions in `NOTICE.md`.
