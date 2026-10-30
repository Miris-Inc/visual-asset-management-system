/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload construct has no unsuppressed AwsSolutions finding when an external asset bucket is
 * registered under a `baseAssetsPrefix`.
 *
 * An imported bucket's ARN comes from config as a literal (`arn:aws:s3:::bucket`), not as a
 * `<AWS::Partition>` token or a GetAtt, so a suppression written only against the created bucket's
 * shapes leaves every grant on the external bucket as a synth error. One synth per file, because
 * cdk-nag reports reliably only on the first synth in a jest process.
 */

import * as s3AssetBuckets from "../../lib/helper/s3AssetBuckets";
import { mirisUploadNagErrors } from "./mirisUploadHarness";

test("the Miris upload construct has no unsuppressed AwsSolutions error with an external asset bucket", () => {
    const { applied, errors } = mirisUploadNagErrors("MirisUploadNagExternalBucketStack", {
        externalBucketPrefix: "vams/data/",
    });
    // Control: the external bucket was registered beside the created one.
    expect(s3AssetBuckets.getS3AssetBucketRecords().map((r) => r.prefix)).toEqual([
        "/",
        "vams/data/",
    ]);
    expect(applied).toBeGreaterThan(0);
    expect(errors).toEqual([]);
});
