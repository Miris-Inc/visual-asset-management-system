/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload construct has no unsuppressed AwsSolutions finding when an external asset bucket is
 * registered at its root (`baseAssetsPrefix` "/"), so its object grants are `arn:aws:s3:::bucket/*`
 * rather than a prefixed key. One synth per file, because cdk-nag reports reliably only on the first
 * synth in a jest process.
 */

import * as s3AssetBuckets from "../../lib/helper/s3AssetBuckets";
import { mirisUploadNagErrors } from "./mirisUploadHarness";

test("the Miris upload construct has no unsuppressed AwsSolutions error with an external bucket at its root", () => {
    const { applied, errors } = mirisUploadNagErrors("MirisUploadNagExternalBucketRootStack", {
        externalBucketPrefix: "/",
    });
    // Control: the external bucket was registered beside the created one.
    expect(s3AssetBuckets.getS3AssetBucketRecords()).toHaveLength(2);
    expect(applied).toBeGreaterThan(0);
    expect(errors).toEqual([]);
});
