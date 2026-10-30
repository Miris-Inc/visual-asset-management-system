/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload construct on the per-pipeline harness, with the storage the deployment gives it.
 *
 * `makePipelineHarness` stubs only the storage most pipelines read. This construct also reads the asset
 * storage table (vamsExecute resolves the asset name), and its claim-prefix suppression names the
 * auxiliary bucket by its storage-stack construct id, so the stub bucket is replaced by one with that
 * id. The KMS key is absent unless asked for, as in the commercial template.
 */

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Annotations, Match } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import * as Config from "../../config/config";
import * as s3AssetBuckets from "../../lib/helper/s3AssetBuckets";
import {
    MirisUploadConstruct,
    MirisUploadConstructProps,
} from "../../lib/nestedStacks/addon/miris/upload/constructs/mirisUpload-construct";
import { PipelineHarness, makePipelineHarness } from "../support/pipelineConstructHarness";

export const MIRIS_TEST_SECRET_ARN =
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf";

export const EXTERNAL_BUCKET_ARN = "arn:aws:s3:::ext-vams-assets";

export interface MirisUploadHarnessOptions {
    /** Give the storage the deployment's customer managed key (`useKmsCmkEncryption`). */
    kms?: boolean;
    /** Register an imported external asset bucket under this `baseAssetsPrefix`. */
    externalBucketPrefix?: string;
    mutate?: (c: Config.Config) => void;
}

export interface MirisUploadHarness extends PipelineHarness {
    construct: MirisUploadConstruct;
    assetAuxiliaryBucket: s3.Bucket;
}

export const makeMirisUploadHarness = (
    id: string,
    options: MirisUploadHarnessOptions = {}
): MirisUploadHarness => {
    const h = makePipelineHarness(id, (c) => {
        c.app.webUi.allowUnsafeEvalFeatures = true;
        c.app.addons.useMirisStreaming.enabled = true;
        c.app.addons.useMirisStreaming.viewerKey = "test-placeholder-viewer-key-000000";
        c.app.addons.useMirisStreaming.apiKeySecretArn = MIRIS_TEST_SECRET_ARN;
        c.app.addons.useMirisStreaming.upload.enabled = true;
        c.app.addons.useMirisStreaming.upload.autoRegisterWithVAMS = false;
        c.app.useKmsCmkEncryption.enabled = options.kms === true;
        options.mutate?.(c);
    });

    const assetAuxiliaryBucket = new s3.Bucket(h.stack, "AssetAuxiliaryBucket");
    const storage = h.storage as any;
    storage.s3.assetAuxiliaryBucket = assetAuxiliaryBucket;
    storage.dynamo = {
        assetStorageTable: new dynamodb.Table(h.stack, "AssetStorageTable", {
            partitionKey: { name: "databaseId", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "assetId", type: dynamodb.AttributeType.STRING },
        }),
    };
    if (options.kms !== true) {
        storage.encryption = { kmsKey: undefined };
    }

    if (options.externalBucketPrefix !== undefined) {
        s3AssetBuckets.addS3AssetBucket(
            s3.Bucket.fromBucketAttributes(h.stack, "ExternalAssetBucket", {
                bucketArn: EXTERNAL_BUCKET_ARN,
            }),
            options.externalBucketPrefix,
            "ext-db"
        );
    }

    const props: MirisUploadConstructProps = {
        config: h.config,
        storageResources: h.storage,
        vpc: h.vpc,
        pipelineSubnets: h.subnets,
        pipelineSecurityGroups: h.securityGroups,
        lambdaCommonBaseLayer: h.lambdaCommonBaseLayer,
        importGlobalPipelineWorkflowV2FunctionName: "importGlobalPipelineWorkflow",
    };
    const construct = new MirisUploadConstruct(h.stack, "MirisUploadPipeline", props);

    return { ...h, construct, assetAuxiliaryBucket };
};

/**
 * Synthesizes the construct under `AwsSolutionsChecks`, as `bin/infra.ts` applies the pack, and
 * returns the unsuppressed AwsSolutions errors on the construct's own resources.
 *
 * Every other harness leaves cdk-nag off, so a suppression that matches nothing surfaces only on a
 * real `cdk synth`. Call it once per test file: cdk-nag reports reliably only on the first synth in a
 * jest process.
 */
export const mirisUploadNagErrors = (id: string, options: MirisUploadHarnessOptions = {}) => {
    const h = makeMirisUploadHarness(id, options);
    // `logIgnores` records each applied suppression as an info annotation, for the control below.
    cdk.Aspects.of(h.stack).add(
        new AwsSolutionsChecks({ verbose: true, logIgnores: true, reports: false })
    );
    const annotations = Annotations.fromStack(h.stack);
    const inConstruct = (m: { id: string }) => m.id.includes("/MirisUploadPipeline/");

    // Control: the aspect ran over the construct. A pack that never visited it would also report no
    // errors, so require suppressions it recorded as applied there.
    const applied = annotations
        .findInfo("*", Match.stringLikeRegexp("AwsSolutions-"))
        .filter(inConstruct);

    // AwsSolutions-L1 is left to the core stack, which suppresses it on every Python 3.12 function it
    // contains (core-stack.ts); this harness builds no core stack.
    const errors = annotations
        .findError("*", Match.stringLikeRegexp("AwsSolutions-"))
        .filter(inConstruct)
        .map((m) => `${m.id}: ${String(m.entry.data).split("\n")[0]}`)
        .filter((line) => !line.includes(": AwsSolutions-L1:"));

    return { harness: h, applied: applied.length, errors };
};
