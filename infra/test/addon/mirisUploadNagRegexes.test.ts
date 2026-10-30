/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload pipeline's IAM5 `appliesTo` patterns match the grants they were written for and no
 * wildcard ARN.
 *
 * A suppression pattern is the one place a broadened grant can pass synth unnoticed: a later edit that
 * widens a resource to `arn:aws:s3:::*\/*` is suppressed silently if the pattern accepts it. Each
 * pattern is compiled the way cdk-nag compiles it and run against the findings a synth produces (the
 * positive controls) and against wildcard ARNs it must never accept (the negative controls).
 */

import {
    MIRIS_UPLOAD_BATCH_JOB_DEFINITION_REGEX,
    MIRIS_UPLOAD_BATCH_JOB_REGEX,
    MIRIS_UPLOAD_JOB_ROLE_OBJECT_REGEXES,
} from "../../lib/nestedStacks/addon/miris/upload/constructs/mirisUpload-construct";
import { MIRIS_CLAIM_PREFIX_OBJECT_REGEX } from "../../lib/nestedStacks/addon/miris/upload/lambdaBuilder/mirisUploadFunctions";

/** cdk-nag's NagSuppressionHelper.toRegEx: the body between the first and last slash, then flags. */
const compile = (pattern: string): RegExp => {
    const m = pattern.match(/\/(.*)\/(.*)?/);
    expect(m).not.toBeNull();
    return new RegExp(m![1], m![2] || "");
};

const matches = (pattern: string, finding: string): boolean => compile(pattern).test(finding);

const ALL_PATTERNS: [string, string][] = [
    ...MIRIS_UPLOAD_JOB_ROLE_OBJECT_REGEXES.map(
        (p, i) => [`job role object ${i}`, p] as [string, string]
    ),
    ["claim prefix object", MIRIS_CLAIM_PREFIX_OBJECT_REGEX],
    ["batch job definition", MIRIS_UPLOAD_BATCH_JOB_DEFINITION_REGEX],
    ["batch job", MIRIS_UPLOAD_BATCH_JOB_REGEX],
];

const WILDCARD_FINDINGS = [
    "arn:aws:s3:::*/*",
    "arn:*:s3:::*/*",
    "arn:aws:s3:::bucket*/*",
    "arn:aws:s3:::*/vams/*",
    "arn:aws:s3:::bucket/vams?/*",
    "arn:aws:s3:::bucket/?/*",
    "<AssetAuxiliaryBucketXYZ.Arn>/*",
    "<AssetAuxiliaryBucket1A2B3C4D.Arn>/*",
    "<AssetAuxiliaryBucket1A2B3C4D.Arn>/locks/*",
    "<AssetAuxiliaryBucket1A2B3C4D.Arn>/locks/miris-upload/../*",
    "<LoggingBucketPolicyXYZ.Arn>/*",
    "<AssetBucket1A2B3C4D.Arn>/locks/miris-upload/*",
    "arn:aws:batch:*:*:job/*",
    "arn:aws:batch:*:*:job-definition/*",
    "arn:<AWS::Partition>:batch:*:123456789012:job/*",
    "*",
].map((arn) => `Resource::${arn}`);

describe("Miris upload IAM5 suppression patterns", () => {
    test.each(ALL_PATTERNS)("%s accepts no wildcard ARN", (_, pattern) => {
        for (const finding of WILDCARD_FINDINGS) {
            expect([finding, matches(pattern, finding)]).toEqual([finding, false]);
        }
    });

    test.each(ALL_PATTERNS)("%s is anchored and carries its leading slash", (_, pattern) => {
        // A regex string without its leading slash parses as a catch-all.
        expect(pattern.startsWith("/^Resource::")).toBe(true);
        expect(pattern).toMatch(/\$\/g$/);
    });

    test("the job role patterns accept the grants the synth produces", () => {
        const accepted = (finding: string) =>
            MIRIS_UPLOAD_JOB_ROLE_OBJECT_REGEXES.some((p) => matches(p, finding));
        for (const finding of [
            "Resource::<AssetBucket1D025086.Arn>/*",
            "Resource::<AssetBucket1D025086.Arn>/pipelines/*",
            "Resource::arn:aws:s3:::ext-vams-assets/*",
            "Resource::arn:aws:s3:::ext-vams-assets/pipelines/*",
            "Resource::arn:aws:s3:::ext-vams-assets/vams/data/*",
            "Resource::arn:aws:s3:::ext-vams-assets/vams/data/pipelines/*",
            "Resource::arn:aws-us-gov:s3:::ext.vams-assets/vams/data/*",
        ]) {
            expect([finding, accepted(finding)]).toEqual([finding, true]);
        }
    });

    test("the claim pattern accepts the claim prefix of the auxiliary bucket only", () => {
        expect(
            matches(
                MIRIS_CLAIM_PREFIX_OBJECT_REGEX,
                "Resource::<AssetAuxiliaryBucket81B76230.Arn>/locks/miris-upload/*"
            )
        ).toBe(true);
        // The storage nested stack's bucket keeps its construct id as the logical-id stem.
        expect(
            matches(
                MIRIS_CLAIM_PREFIX_OBJECT_REGEX,
                "Resource::<StorageAssetAuxiliaryBucketA1B2C3D4.Arn>/locks/miris-upload/*"
            )
        ).toBe(true);
        expect(
            matches(
                MIRIS_CLAIM_PREFIX_OBJECT_REGEX,
                "Resource::<AssetAuxiliaryBucket81B76230.Arn>/locks/other/*"
            )
        ).toBe(false);
    });

    test("the batch patterns accept this deployment's Region and account only", () => {
        expect(
            matches(
                MIRIS_UPLOAD_BATCH_JOB_REGEX,
                "Resource::arn:aws:batch:us-east-1:123456789012:job/*"
            )
        ).toBe(true);
        expect(
            matches(
                MIRIS_UPLOAD_BATCH_JOB_DEFINITION_REGEX,
                "Resource::arn:<AWS::Partition>:batch:us-east-1:123456789012:job-definition/*"
            )
        ).toBe(true);
        expect(
            matches(
                MIRIS_UPLOAD_BATCH_JOB_DEFINITION_REGEX,
                "Resource::arn:<AWS::Partition>:batch:<AWS::Region>:<AWS::AccountId>:job-definition/*"
            )
        ).toBe(true);
        expect(
            matches(
                MIRIS_UPLOAD_BATCH_JOB_REGEX,
                "Resource::arn:aws-us-gov:batch:us-gov-west-1:123456789012:job/*"
            )
        ).toBe(true);
    });
});
