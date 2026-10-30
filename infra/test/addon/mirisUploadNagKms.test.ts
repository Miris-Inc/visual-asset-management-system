/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload construct has no unsuppressed AwsSolutions finding with the customer managed KMS
 * key, which adds the key statements to every role that reads or writes encrypted storage. One synth
 * per file, because cdk-nag reports reliably only on the first synth in a jest process.
 */

import { mirisUploadNagErrors } from "./mirisUploadHarness";

test("the Miris upload construct has no unsuppressed AwsSolutions error with a customer managed key", () => {
    const { harness, applied, errors } = mirisUploadNagErrors("MirisUploadNagKmsStack", {
        kms: true,
    });
    // Control: the key really reached the construct's storage.
    expect(harness.storage.encryption.kmsKey).toBeDefined();
    expect(applied).toBeGreaterThan(0);
    expect(errors).toEqual([]);
});
