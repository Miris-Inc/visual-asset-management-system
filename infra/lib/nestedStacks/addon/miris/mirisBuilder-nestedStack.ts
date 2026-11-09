/*
 * Miris Spatial Streaming add-on nested stack.
 *
 * Serves the Miris stream status and viewer key routes the streaming viewer calls, seeds the GLOBAL
 * "Miris stream" file metadata schema, and in perView mode records each minted viewer key so that
 * an expired key is deleted from the Miris workspace.
 *
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

import { Construct } from "constructs";
import * as cdk from "aws-cdk-lib";
import { NestedStack } from "aws-cdk-lib";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { LayerVersion } from "aws-cdk-lib/aws-lambda";
import { SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { NagSuppressions } from "cdk-nag";
import * as Config from "../../../../config/config";
import { ADDON_RESOURCE_PARAM_KEYS } from "../../../../common/addonResourceParamKeys";
import { Service } from "../../../helper/service-helper";
import {
    NAG_REASON_LAMBDA_BASIC_EXECUTION,
    kmsKeyPolicyStatementGenerator,
} from "../../../helper/security";
import { RouteRegistry, attachFunctionToApi } from "../../apiLambda/apiRouteRegistry";
import { storageResources } from "../../storage/storageBuilder-nestedStack";
import { publishAddonResourceName } from "../addonStackHelpers";
import { DynamoDbMetadataSchemaMirisConstruct } from "./constructs/dynamodb-metadataschema-miris-construct";
import {
    buildGetMirisAssetStatusFunction,
    buildMintMirisViewerKeyFunction,
    buildMirisViewerKeyCleanupFunction,
} from "./lambdaBuilder/mirisFunctions";

export interface MirisBuilderNestedStackProps extends cdk.StackProps {
    config: Config.Config;
    vpc: ec2.IVpc;
    isolatedSubnets: ec2.ISubnet[];
    storageResources: storageResources;
    lambdaCommonBaseLayer: LayerVersion;
    registry: RouteRegistry;
}

const defaultProps: Partial<MirisBuilderNestedStackProps> = {};

export class MirisBuilderNestedStack extends NestedStack {
    constructor(parent: Construct, name: string, props: MirisBuilderNestedStackProps) {
        super(parent, name);
        props = { ...defaultProps, ...props };

        const perView = props.config.app.addons.useMirisStreaming.viewerKeyMode === "perView";

        // The shared VAMS CMK, imported by ARN. The grants below on the CMK-encrypted table and
        // queue then write only to the grantee's own policy; a grant on the Storage stack's key
        // object can instead add the grantee to that key's resource policy, which makes the Storage
        // stack consume this stack's output while this stack consumes Storage's, a CloudFormation
        // circular dependency.
        const sharedKmsKey = props.storageResources.encryption.kmsKey;
        const kmsKey = sharedKmsKey
            ? kms.Key.fromKeyArn(this, "MirisKmsKeyRef", sharedKmsKey.keyArn)
            : undefined;

        // Per-view viewer key IDs. TTL deletions stream to the cleanup Lambda, which deletes the
        // key from the Miris workspace.
        let viewerKeyTable: dynamodb.Table | undefined;
        if (perView) {
            viewerKeyTable = new dynamodb.Table(this, "MirisViewerKeyStorageTable", {
                partitionKey: {
                    name: "mirisViewerKeyId",
                    type: dynamodb.AttributeType.STRING,
                },
                timeToLiveAttribute: "ttl",
                stream: dynamodb.StreamViewType.KEYS_ONLY,
                contributorInsightsSpecification: {
                    enabled: false,
                },
                pointInTimeRecoverySpecification: {
                    pointInTimeRecoveryEnabled: true,
                },
                // RETAIN and auto-named, as every VAMS table, so a retained orphan never collides
                // with the table a redeploy creates.
                removalPolicy: cdk.RemovalPolicy.RETAIN,
                billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
                encryption: kmsKey
                    ? dynamodb.TableEncryption.CUSTOMER_MANAGED
                    : dynamodb.TableEncryption.AWS_MANAGED,
                encryptionKey: kmsKey,
            });

            publishAddonResourceName(
                this,
                props.config,
                ADDON_RESOURCE_PARAM_KEYS.dynamoTables.mirisViewerKeyStorage,
                viewerKeyTable.tableName
            );
        }

        // Miris stream status for a file, read from that file's metadata (streaming viewer)
        const getAssetStatusFunction = buildGetMirisAssetStatusFunction(
            this,
            props.lambdaCommonBaseLayer,
            props.storageResources,
            props.config,
            props.vpc,
            props.isolatedSubnets
        );
        attachFunctionToApi(this, getAssetStatusFunction, {
            routePath: "/addon/miris/asset-status",
            method: apigwv2.HttpMethod.GET,
            registry: props.registry,
        });

        // Viewer key for a file: the deployment key (static) or one minted per view (perView)
        const mintViewerKeyFunction = buildMintMirisViewerKeyFunction(
            this,
            props.lambdaCommonBaseLayer,
            props.storageResources,
            props.config,
            props.vpc,
            props.isolatedSubnets,
            viewerKeyTable
        );
        attachFunctionToApi(this, mintViewerKeyFunction, {
            routePath: "/addon/miris/viewer-key",
            method: apigwv2.HttpMethod.POST,
            registry: props.registry,
        });

        // Per-view viewer keys: delete each key from Miris when TTL removes its table row
        if (viewerKeyTable) {
            const viewerKeyCleanupFunction = buildMirisViewerKeyCleanupFunction(
                this,
                props.lambdaCommonBaseLayer,
                props.storageResources,
                props.config,
                props.vpc,
                props.isolatedSubnets,
                viewerKeyTable
            );

            const viewerKeyCleanupDlq = new sqs.Queue(this, "MirisViewerKeyCleanupDLQ", {
                retentionPeriod: cdk.Duration.days(14),
                encryption: kmsKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
                encryptionMasterKey: kmsKey,
                enforceSSL: true,
            });

            // Only TTL deletions: REMOVE records written by the DynamoDB service itself. A batch
            // that still fails after its retries is recorded in the dead-letter queue.
            new lambda.EventSourceMapping(this, "MirisViewerKeyCleanupStream", {
                target: viewerKeyCleanupFunction,
                eventSourceArn: viewerKeyTable.tableStreamArn,
                startingPosition: lambda.StartingPosition.TRIM_HORIZON,
                batchSize: 5,
                reportBatchItemFailures: true,
                bisectBatchOnError: true,
                retryAttempts: 10,
                onFailure: new SqsDlq(viewerKeyCleanupDlq),
                filters: [
                    lambda.FilterCriteria.filter({
                        eventName: lambda.FilterRule.isEqual("REMOVE"),
                        userIdentity: {
                            type: lambda.FilterRule.isEqual("Service"),
                            principalId: lambda.FilterRule.isEqual("dynamodb.amazonaws.com"),
                        },
                    }),
                ],
            });

            NagSuppressions.addResourceSuppressions(
                viewerKeyCleanupDlq,
                [
                    {
                        id: "AwsSolutions-SQS3",
                        reason: "This queue is the on-failure destination of the Miris viewer key cleanup stream mapping and so terminates that chain; a redrive policy of its own would only move the same failed batch record to a further queue.",
                    },
                ],
                true
            );
        }

        // GLOBAL "Miris stream" file metadata schema, written by a custom resource on a role that
        // can only put items into the metadata schema table.
        const schemaCustomResourceRole = new iam.Role(this, "MirisStreamSchemaCustomResourceRole", {
            assumedBy: Service("LAMBDA").Principal,
            inlinePolicies: {
                TablePolicy: new iam.PolicyDocument({
                    statements: [
                        new iam.PolicyStatement({
                            effect: iam.Effect.ALLOW,
                            actions: ["dynamodb:PutItem"],
                            resources: [
                                props.storageResources.dynamo.metadataSchemaStorageTableV2.tableArn,
                            ],
                        }),
                    ],
                }),
                ...(kmsKey
                    ? {
                          KmsPolicy: new iam.PolicyDocument({
                              statements: [kmsKeyPolicyStatementGenerator(kmsKey)],
                          }),
                      }
                    : {}),
            },
            managedPolicies: [
                iam.ManagedPolicy.fromAwsManagedPolicyName(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        });
        new DynamoDbMetadataSchemaMirisConstruct(this, "MetadataSchemaMiris", {
            customResourceRole: schemaCustomResourceRole,
            storageResources: props.storageResources,
        });

        NagSuppressions.addResourceSuppressions(
            schemaCustomResourceRole,
            [
                {
                    id: "AwsSolutions-IAM4",
                    reason: NAG_REASON_LAMBDA_BASIC_EXECUTION,
                    appliesTo: [{ regex: "/.*AWSLambdaBasicExecutionRole$/g" }],
                },
                {
                    id: "AwsSolutions-IAM5",
                    reason: "Allow permissions for KMS unencryption/re-encryption for keys generated within VAMS. Policy statements additions on imported keys are No-Op statements and must be set externally to the deployment.",
                    appliesTo: [{ regex: "/^Action::kms:(.*)\\*$/g" }],
                },
            ],
            true
        );
    }
}
