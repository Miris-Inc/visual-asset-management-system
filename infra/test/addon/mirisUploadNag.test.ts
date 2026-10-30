/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload construct synthesizes with no unsuppressed AwsSolutions finding, with the asset
 * bucket this deployment creates and no customer managed key (the commercial template's defaults).
 *
 * The customer-managed-key and external-bucket variants are their own files
 * (`mirisUploadNag*.test.ts`): one synth per file, because cdk-nag reports reliably only on the first
 * synth in a jest process.
 */

import { mirisUploadNagErrors } from "./mirisUploadHarness";

test("the Miris upload construct has no unsuppressed AwsSolutions error", () => {
    const { applied, errors } = mirisUploadNagErrors("MirisUploadNagStack");
    expect(applied).toBeGreaterThan(0);
    expect(errors).toEqual([]);
});
