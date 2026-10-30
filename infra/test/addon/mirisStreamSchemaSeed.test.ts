/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The GLOBAL "Miris stream" file metadata schema is seeded like the core default schemas
 * (`metadataSchemaDefaultsSeed.test.ts`): create writes it unless an edited row already exists,
 * update rewrites the row only while it exists and its `modifiedBy` is still `SYSTEM_USER`, and both
 * ignore the conditional-check failure a skipped write raises. The calls carry no synth-time value,
 * so a deployment that changes nothing here sends CloudFormation no update for them.
 */

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { DynamoDbMetadataSchemaMirisConstruct } from "../../lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-construct";
import mirisStreamSchema from "../../lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-stream.json";
import { newTestApp } from "../support/testApp";

const TABLE = "metadata-schema-table";

function synth(): Template {
    const app = newTestApp();
    const stack = new cdk.Stack(app, "MirisStreamSchemaSeedTestStack");
    const customResourceRole = new iam.Role(stack, "CustomResourceRole", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    new DynamoDbMetadataSchemaMirisConstruct(stack, "MetadataSchemaMiris", {
        customResourceRole,
        storageResources: {
            dynamo: { metadataSchemaStorageTableV2: { tableName: TABLE } },
        } as any,
    });
    return Template.fromStack(stack);
}

/** The seed resource's Create and Update payloads, parsed. */
function seed(template: Template): { create: any; update: any; raw: any } {
    const resources = Object.values(template.findResources("Custom::AWS"));
    expect(resources).toHaveLength(1);
    const r: any = resources[0];
    return {
        create: JSON.parse(r.Properties.Create),
        update: JSON.parse(r.Properties.Update),
        raw: r.Properties,
    };
}

describe("Miris stream metadata schema seeding", () => {
    const { create, update, raw } = seed(synth());

    test("the Miris stream schema is the item seeded (control)", () => {
        expect(create.action).toBe("putItem");
        expect(create.parameters.TableName).toBe(TABLE);
        expect(create.parameters.Item.metadataSchemaId).toEqual({
            S: mirisStreamSchema.metadataSchemaId,
        });
    });

    test("create writes only a missing or unedited schema", () => {
        expect(create.parameters.ConditionExpression).toBe(
            "attribute_not_exists(metadataSchemaId) OR modifiedBy = :systemUser"
        );
        expect(create.ignoreErrorCodesMatching).toBe("ConditionalCheckFailedException");
    });

    test("update refreshes only an existing, unedited schema", () => {
        expect(update.parameters.ConditionExpression).toBe(
            "attribute_exists(metadataSchemaId) AND modifiedBy = :systemUser"
        );
        expect(update.ignoreErrorCodesMatching).toBe("ConditionalCheckFailedException");
        expect(update.parameters.Item).toEqual(create.parameters.Item);
    });

    test("the seeding identity is SYSTEM_USER, matched and written", () => {
        for (const call of [create, update]) {
            expect(call.parameters.ExpressionAttributeValues).toEqual({
                ":systemUser": { S: "SYSTEM_USER" },
            });
            expect(call.parameters.Item.modifiedBy).toEqual({ S: "SYSTEM_USER" });
            expect(call.parameters.Item.createdBy).toEqual({ S: "SYSTEM_USER" });
        }
    });

    test("the item carries no synth-time timestamp", () => {
        expect(create.parameters.Item.dateCreated).toBeUndefined();
        expect(create.parameters.Item.dateModified).toBeUndefined();
    });

    test("a second synth produces identical seed calls", () => {
        expect(seed(synth()).raw).toEqual(raw);
    });
});
