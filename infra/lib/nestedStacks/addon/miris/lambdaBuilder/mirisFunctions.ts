/*
 * Miris Spatial Streaming add-on Lambda function builders.
 *
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

import * as lambda from "aws-cdk-lib/aws-lambda";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as path from "path";
import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import { LayerVersion } from "aws-cdk-lib/aws-lambda";
import * as Config from "../../../../../config/config";
import { LAMBDA_PYTHON_RUNTIME } from "../../../../../config/config";
import { storageResources } from "../../../storage/storageBuilder-nestedStack";
import {
    globalLambdaEnvironmentsAndPermissions,
    kmsKeyLambdaPermissionAddToResourcePolicy,
    setupSecurityAndLoggingEnvironmentAndPermissions,
    suppressCdkNagDynamoStreamListWildcard,
    suppressCdkNagErrorsByGrantReadWrite,
    suppressCdkNagLambda,
} from "../../../../helper/security";

const BACKEND_CODE_PATH = path.join(__dirname, "../../../../../../backend/backend");

/** Read access to exactly the configured Miris Integration Key secret. */
function mirisIntegrationKeyReadStatement(config: Config.Config): iam.PolicyStatement {
    return new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [config.app.addons.useMirisStreaming.apiKeySecretArn],
    });
}

/**
 * The viewer key settings the status and viewer key handlers read. The deployment viewer key is
 * set in static mode only; perView mode has none.
 */
function mirisViewerKeyEnvironment(config: Config.Config): Record<string, string> {
    const miris = config.app.addons.useMirisStreaming;
    return {
        MIRIS_VIEWER_KEY_MODE: miris.viewerKeyMode,
        ...(miris.viewerKeyMode === "static" ? { MIRIS_VIEWER_KEY: miris.viewerKey } : {}),
        MIRIS_API_KEY_SECRET_ARN: miris.apiKeySecretArn,
        MIRIS_API_BASE_URL: miris.mirisApiBaseUrl,
    };
}

function lambdaVpcProps(
    config: Config.Config,
    vpc: ec2.IVpc,
    subnets: ec2.ISubnet[]
): Pick<lambda.FunctionProps, "vpc" | "vpcSubnets"> {
    const inVpc = config.app.useGlobalVpc.enabled && config.app.useGlobalVpc.useForAllLambdas;
    return {
        vpc: inVpc ? vpc : undefined,
        vpcSubnets: inVpc ? { subnets: subnets } : undefined,
    };
}

/** Backs GET /addon/miris/asset-status: the Miris stream status of one asset file. */
export function buildGetMirisAssetStatusFunction(
    scope: Construct,
    lambdaCommonBaseLayer: LayerVersion,
    storageResources: storageResources,
    config: Config.Config,
    vpc: ec2.IVpc,
    subnets: ec2.ISubnet[]
): lambda.Function {
    const name = "getAssetStatus";
    const fun = new lambda.Function(scope, "MirisGetAssetStatusFunction", {
        code: lambda.Code.fromAsset(BACKEND_CODE_PATH),
        handler: `handlers.addon.miris.${name}.lambda_handler`,
        runtime: LAMBDA_PYTHON_RUNTIME,
        layers: [lambdaCommonBaseLayer],
        timeout: Duration.seconds(30),
        memorySize: Config.LAMBDA_MEMORY_SIZE,
        ...lambdaVpcProps(config, vpc, subnets),
        environment: mirisViewerKeyEnvironment(config),
    });

    // Read-only: the asset record for Tier-2 authorization, and the Miris stream keys
    // stored as file metadata on the viewed file.
    storageResources.dynamo.assetStorageTable.grantReadData(fun);
    storageResources.dynamo.assetFileMetadataStorageTable.grantReadData(fun);

    // The Miris Integration Key, which authorizes the asset state and tag reads.
    fun.addToRolePolicy(mirisIntegrationKeyReadStatement(config));

    kmsKeyLambdaPermissionAddToResourcePolicy(fun, storageResources.encryption.kmsKey);
    setupSecurityAndLoggingEnvironmentAndPermissions(fun, storageResources);
    globalLambdaEnvironmentsAndPermissions(fun, config);
    suppressCdkNagLambda(fun);
    suppressCdkNagErrorsByGrantReadWrite(fun);

    return fun;
}

/**
 * Backs POST /addon/miris/viewer-key: the deployment viewer key in static mode, or a viewer key
 * minted for one asset file in perView mode, whose key ID is recorded in `viewerKeyTable`.
 */
export function buildMintMirisViewerKeyFunction(
    scope: Construct,
    lambdaCommonBaseLayer: LayerVersion,
    storageResources: storageResources,
    config: Config.Config,
    vpc: ec2.IVpc,
    subnets: ec2.ISubnet[],
    viewerKeyTable: dynamodb.ITable | undefined
): lambda.Function {
    const perView = config.app.addons.useMirisStreaming.viewerKeyMode === "perView";
    if (perView && !viewerKeyTable) {
        throw new Error(
            'buildMintMirisViewerKeyFunction requires the viewer key table in "perView" mode.'
        );
    }

    const name = "mintViewerKey";
    const fun = new lambda.Function(scope, "MirisMintViewerKeyFunction", {
        code: lambda.Code.fromAsset(BACKEND_CODE_PATH),
        handler: `handlers.addon.miris.${name}.lambda_handler`,
        runtime: LAMBDA_PYTHON_RUNTIME,
        layers: [lambdaCommonBaseLayer],
        timeout: Duration.seconds(30),
        memorySize: Config.LAMBDA_MEMORY_SIZE,
        ...lambdaVpcProps(config, vpc, subnets),
        environment: {
            ...mirisViewerKeyEnvironment(config),
            MIRIS_VIEWER_KEY_TTL_MINUTES: String(
                config.app.addons.useMirisStreaming.viewerKeyTtlMinutes
            ),
        },
    });

    // The Miris Integration Key, which authorizes the asset tag read and, in perView mode, the
    // viewer key creates (and the delete of a key whose table row could not be written).
    fun.addToRolePolicy(mirisIntegrationKeyReadStatement(config));

    // Read-only: the asset record for Tier-2 authorization, and the Miris stream keys
    // stored as file metadata on the file.
    storageResources.dynamo.assetStorageTable.grantReadData(fun);
    storageResources.dynamo.assetFileMetadataStorageTable.grantReadData(fun);

    // Put-only: one row per minted key. The table has no index.
    if (perView && viewerKeyTable) {
        viewerKeyTable.grant(fun, "dynamodb:PutItem");
    }

    kmsKeyLambdaPermissionAddToResourcePolicy(fun, storageResources.encryption.kmsKey);
    setupSecurityAndLoggingEnvironmentAndPermissions(fun, storageResources);
    globalLambdaEnvironmentsAndPermissions(fun, config);
    suppressCdkNagLambda(fun);
    suppressCdkNagErrorsByGrantReadWrite(fun);

    return fun;
}

/**
 * Deletes a per-view Miris viewer key from the Miris workspace when DynamoDB TTL removes its row
 * from `viewerKeyTable`. The caller attaches the table's stream with an event source mapping.
 */
export function buildMirisViewerKeyCleanupFunction(
    scope: Construct,
    lambdaCommonBaseLayer: LayerVersion,
    storageResources: storageResources,
    config: Config.Config,
    vpc: ec2.IVpc,
    subnets: ec2.ISubnet[],
    viewerKeyTable: dynamodb.ITable
): lambda.Function {
    const name = "viewerKeyCleanup";
    const fun = new lambda.Function(scope, "MirisViewerKeyCleanupFunction", {
        code: lambda.Code.fromAsset(BACKEND_CODE_PATH),
        handler: `handlers.addon.miris.${name}.lambda_handler`,
        runtime: LAMBDA_PYTHON_RUNTIME,
        layers: [lambdaCommonBaseLayer],
        timeout: Duration.minutes(5),
        memorySize: Config.LAMBDA_MEMORY_SIZE,
        ...lambdaVpcProps(config, vpc, subnets),
        environment: {
            MIRIS_API_KEY_SECRET_ARN: config.app.addons.useMirisStreaming.apiKeySecretArn,
            MIRIS_API_BASE_URL: config.app.addons.useMirisStreaming.mirisApiBaseUrl,
        },
    });

    // The Miris Integration Key, which authorizes the viewer key deletes.
    fun.addToRolePolicy(mirisIntegrationKeyReadStatement(config));

    // The table's stream, which carries the key IDs of TTL-deleted rows.
    viewerKeyTable.grantStreamRead(fun);

    kmsKeyLambdaPermissionAddToResourcePolicy(fun, storageResources.encryption.kmsKey);
    setupSecurityAndLoggingEnvironmentAndPermissions(fun, storageResources);
    globalLambdaEnvironmentsAndPermissions(fun, config);
    suppressCdkNagDynamoStreamListWildcard(fun);
    suppressCdkNagLambda(fun);
    suppressCdkNagErrorsByGrantReadWrite(fun);

    return fun;
}
