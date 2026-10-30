/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris Spatial Streaming add-on stack, synthesized from the commercial template.
 *
 * With `app.addons.useMirisStreaming` off, the deployment carries no Miris resource at all. With it on,
 * the add-on nested stack registers `GET /addon/miris/asset-status` and `POST /addon/miris/viewer-key`,
 * seeds the GLOBAL "Miris stream" file metadata schema, and publishes the `MIRIS_ADDON` switch; the
 * basic read-only role is seeded with both routes, and the web policy admits the Miris hosts.
 *
 * `viewerKeyMode` decides the rest. In "static" mode the deployment viewer key is set on the status
 * and viewer key Lambdas and nothing records keys. In "perView" mode no viewer key is configured; the
 * viewer key Lambda mints one per view and records its ID in the add-on's viewer key table, whose TTL
 * deletions stream to a cleanup Lambda through a filtered event source mapping with a dead-letter
 * queue. The perView synth runs with the deployment CMK, which the add-on stack imports by ARN.
 *
 * Every Lambda reads exactly the configured Integration Key secret, and only the viewer key Lambda
 * writes to the viewer key table, and only PutItem.
 */

import mirisStreamSchema from "../../lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-stream.json";
import { Resource, SynthResult, synthTemplate } from "../support/templateSynth";

const STATUS_ROUTE = "/addon/miris/asset-status";
const VIEWER_KEY_ROUTE = "/addon/miris/viewer-key";
const STATUS_HANDLER = "handlers.addon.miris.getAssetStatus.lambda_handler";
const MINT_HANDLER = "handlers.addon.miris.mintViewerKey.lambda_handler";
const CLEANUP_HANDLER = "handlers.addon.miris.viewerKeyCleanup.lambda_handler";
const SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf";
const PLACEHOLDER_VIEWER_KEY = "test-placeholder-viewer-key-000000";
const PARAM_SUFFIX = "/resourceNames/addon/miris/viewerKeyStorage";
const SCHEMA_ID = "miris-stream-file-schema";
const WRITE_ACTION = /^dynamodb:(PutItem|UpdateItem|DeleteItem|BatchWriteItem|\*)$/;

function enableMiris(viewerKeyMode: "static" | "perView", kms: boolean) {
    return (c: any) => {
        c.app.webUi.allowUnsafeEvalFeatures = true;
        c.app.useKmsCmkEncryption.enabled = kms;
        const miris = c.app.addons.useMirisStreaming;
        miris.enabled = true;
        miris.viewerKeyMode = viewerKeyMode;
        miris.viewerKey = viewerKeyMode === "static" ? PLACEHOLDER_VIEWER_KEY : "";
        miris.viewerKeyTtlMinutes = 7;
        miris.apiKeySecretArn = SECRET_ARN;
    };
}

function restApiPaths(synth: SynthResult): Record<string, Record<string, unknown>> {
    const apis = synth.ofType("AWS::ApiGateway::RestApi");
    expect(apis).toHaveLength(1);
    return (apis[0].properties as any).Body.paths;
}

function functionsWithHandler(synth: SynthResult, handler: string): Resource[] {
    return synth.where("AWS::Lambda::Function", (r) => r.properties.Handler === handler);
}

function onlyFunction(synth: SynthResult, handler: string): Resource {
    const fns = functionsWithHandler(synth, handler);
    expect(fns).toHaveLength(1);
    return fns[0];
}

/** The Miris variables a Lambda carries; the shared helpers add their own beside them. */
function mirisEnvironment(fn: Resource): Record<string, string> {
    const variables: Record<string, string> = fn.properties.Environment?.Variables ?? {};
    return Object.fromEntries(Object.entries(variables).filter(([k]) => k.startsWith("MIRIS_")));
}

/** Every IAM statement attached to the execution role of `fn`. */
function roleStatements(synth: SynthResult, fn: Resource): any[] {
    const roleLogicalId = fn.properties.Role["Fn::GetAtt"][0];
    return synth
        .where(
            "AWS::IAM::Policy",
            (p) =>
                p.stack === fn.stack &&
                (p.properties.Roles ?? []).some((role: any) => role?.Ref === roleLogicalId)
        )
        .flatMap((p) => p.properties.PolicyDocument.Statement as any[]);
}

function actionsOf(statement: any): string[] {
    return Array.isArray(statement.Action) ? statement.Action : [statement.Action];
}

function expectExactSecretGrant(statements: any[]) {
    const secrets = statements.filter((s) =>
        actionsOf(s).some((a) => a.toLowerCase().startsWith("secretsmanager:"))
    );
    expect(secrets).toHaveLength(1);
    expect(secrets[0].Effect).toBe("Allow");
    expect(actionsOf(secrets[0])).toEqual(["secretsmanager:GetSecretValue"]);
    expect(secrets[0].Resource).toBe(SECRET_ARN);
}

function viewerKeyTables(synth: SynthResult): Resource[] {
    return synth.where("AWS::DynamoDB::Table", (r) =>
        (r.properties.KeySchema ?? []).some((k: any) => k.AttributeName === "mirisViewerKeyId")
    );
}

function viewerKeyParams(synth: SynthResult): Resource[] {
    return synth.where("AWS::SSM::Parameter", (r) =>
        SynthResult.flatten(r.properties.Name).endsWith(PARAM_SUFFIX)
    );
}

function cleanupMappings(synth: SynthResult): Resource[] {
    const fnIds = functionsWithHandler(synth, CLEANUP_HANDLER).map((f) => f.logicalId);
    return synth.where("AWS::Lambda::EventSourceMapping", (r) =>
        fnIds.includes(r.properties.FunctionName?.Ref)
    );
}

/** The putItem call a custom resource makes on create, with tokens flattened. */
function createCall(resource: Resource): any {
    return JSON.parse(SynthResult.flatten(resource.properties.Create));
}

function schemaSeeds(synth: SynthResult): Resource[] {
    return synth
        .ofType("Custom::AWS")
        .filter((r) => createCall(r).parameters?.Item?.metadataSchemaId?.S === SCHEMA_ID);
}

/** The read-only role's seeded constraint item whose id contains `constraintSuffix`. */
function roleConstraintCriteria(synth: SynthResult, constraintSuffix: string): any[] {
    const items = synth
        .ofType("Custom::AWS")
        .map((r) => createCall(r).parameters?.Item)
        .filter((item) => item?.constraintId?.S?.includes(`_basicro_${constraintSuffix}`));
    expect(items).toHaveLength(1);
    return JSON.parse(items[0].criteriaOr.S);
}

/** Whether the feature-switch custom resource records `feature`. */
function recordsFeature(synth: SynthResult, feature: string): boolean {
    return synth
        .ofType("Custom::AWS")
        .some((r) => JSON.stringify(r.properties.Create).includes(`-${feature}\\"`));
}

/** The CloudFront response-headers Content-Security-Policy, flattened. */
function contentSecurityPolicy(synth: SynthResult): string {
    const policies = synth
        .ofType("AWS::CloudFront::ResponseHeadersPolicy")
        .map((r) =>
            SynthResult.flatten(
                r.properties.ResponseHeadersPolicyConfig?.SecurityHeadersConfig
                    ?.ContentSecurityPolicy?.ContentSecurityPolicy
            )
        );
    const withCsp = policies.filter((p) => p.includes("connect-src"));
    expect(withCsp).toHaveLength(1);
    return withCsp[0];
}

function directive(csp: string, name: string): string[] {
    const found = csp.split(";").find((d) => d.trim().startsWith(`${name} `));
    return (found ?? "").trim().split(/\s+/).slice(1);
}

describe("Miris add-on disabled (stock commercial template)", () => {
    let synth: SynthResult;

    beforeAll(() => {
        synth = synthTemplate("commercial");
    });

    test("[control] the add-on nested stack is synthesized", () => {
        expect(
            synth.where("AWS::Lambda::Function", (r) =>
                r.logicalId.startsWith("AddonNestedStackEmptyLambda")
            )
        ).toHaveLength(1);
        expect(Object.keys(restApiPaths(synth)).length).toBeGreaterThan(50);
    });

    test("no resource in any template names Miris", () => {
        expect(synth.grep(/miris/i)).toEqual([]);
    });

    test("neither route is registered and the switch is not recorded", () => {
        const paths = restApiPaths(synth);
        expect(paths[STATUS_ROUTE]).toBeUndefined();
        expect(paths[VIEWER_KEY_ROUTE]).toBeUndefined();
        expect(recordsFeature(synth, "MIRIS_ADDON")).toBe(false);
    });
});

describe("Miris add-on enabled, viewerKeyMode static", () => {
    let synth: SynthResult;

    beforeAll(() => {
        synth = synthTemplate("commercial", {
            mutate: enableMiris("static", false),
            mutateKey: "miris-addon-static",
        });
    });

    test("both routes are registered, each for its one method", () => {
        const paths = restApiPaths(synth);
        const methods = (path: string) =>
            Object.keys(paths[path] ?? {}).filter((m) => m !== "options");
        expect(methods(STATUS_ROUTE)).toEqual(["get"]);
        expect(methods(VIEWER_KEY_ROUTE)).toEqual(["post"]);
    });

    test("the MIRIS_ADDON switch is recorded", () => {
        expect(recordsFeature(synth, "MIRIS_ADDON")).toBe(true);
        // Control: the detector reads the same record for a core switch the template enables.
        expect(recordsFeature(synth, "CLOUDFRONTDEPLOY")).toBe(true);
    });

    test("the status Lambda carries the viewer key settings and the Integration Key ARN", () => {
        const fn = onlyFunction(synth, STATUS_HANDLER);
        expect(mirisEnvironment(fn)).toEqual({
            MIRIS_VIEWER_KEY_MODE: "static",
            MIRIS_VIEWER_KEY: PLACEHOLDER_VIEWER_KEY,
            MIRIS_API_KEY_SECRET_ARN: SECRET_ARN,
            MIRIS_API_BASE_URL: "https://app.miris.com",
        });
        // Table names resolve through SSM, not per-table environment variables.
        const variables = fn.properties.Environment.Variables;
        expect(variables.VAMS_RESOURCE_PARAM_PREFIX).toBeDefined();
        expect(Object.keys(variables).filter((k) => k.endsWith("_TABLE_NAME"))).toEqual([]);
    });

    test("the viewer key Lambda also carries the key TTL", () => {
        expect(mirisEnvironment(onlyFunction(synth, MINT_HANDLER))).toEqual({
            MIRIS_VIEWER_KEY_MODE: "static",
            MIRIS_VIEWER_KEY: PLACEHOLDER_VIEWER_KEY,
            MIRIS_API_KEY_SECRET_ARN: SECRET_ARN,
            MIRIS_API_BASE_URL: "https://app.miris.com",
            MIRIS_VIEWER_KEY_TTL_MINUTES: "7",
        });
    });

    test.each([STATUS_HANDLER, MINT_HANDLER])(
        "%s reads exactly the secret and writes no DynamoDB data",
        (handler) => {
            const statements = roleStatements(synth, onlyFunction(synth, handler));
            expectExactSecretGrant(statements);
            expect(statements.flatMap(actionsOf).filter((a) => WRITE_ACTION.test(a))).toEqual([]);
            const reads = JSON.stringify(
                statements.filter((s) => actionsOf(s).includes("dynamodb:GetItem"))
            );
            expect(reads).toMatch(/AssetStorageTable/);
            expect(reads).toMatch(/AssetFileMetadataStorageTableV2/);
        }
    );

    test("no viewer key table, parameter, cleanup Lambda, mapping or queue is deployed", () => {
        expect(viewerKeyTables(synth)).toEqual([]);
        expect(viewerKeyParams(synth)).toEqual([]);
        expect(functionsWithHandler(synth, CLEANUP_HANDLER)).toEqual([]);
        expect(synth.grep("MirisViewerKeyCleanup")).toEqual([]);
        expect(synth.grep("MirisViewerKeyStorageTable")).toEqual([]);
    });

    test("the viewer key appears in no stack output", () => {
        const outputs = Object.values(synth.templates).map((t: any) =>
            JSON.stringify(t.Outputs ?? {})
        );
        expect(outputs.filter((o) => o.includes(PLACEHOLDER_VIEWER_KEY))).toEqual([]);
        // Control: the key is in the template, on the two Lambdas that return or use it.
        expect(synth.grep(PLACEHOLDER_VIEWER_KEY)).toHaveLength(2);
    });

    test("the basic read-only role is seeded with both routes", () => {
        expect(roleConstraintCriteria(synth, "allow_get_apis")).toContainEqual({
            field: "route__path",
            id: "17_basicro_api_paths",
            operator: "equals",
            value: STATUS_ROUTE,
        });
        expect(roleConstraintCriteria(synth, "allow_post_apis")).toContainEqual({
            field: "route__path",
            id: "17_basicro_api_paths_post",
            operator: "equals",
            value: VIEWER_KEY_ROUTE,
        });
    });

    test("the web policy admits the Miris hosts and blob: scripts", () => {
        const csp = contentSecurityPolicy(synth);
        expect(directive(csp, "connect-src")).toEqual(
            expect.arrayContaining(["https://*.miris.com", "wss://*.miris.com"])
        );
        expect(directive(csp, "script-src")).toContain("blob:");
    });

    describe("Miris stream metadata schema", () => {
        test("exactly one custom resource seeds it", () => {
            expect(schemaSeeds(synth)).toHaveLength(1);
            expect(synth.grep(SCHEMA_ID)).toHaveLength(1);
        });

        test("create and update write the GLOBAL fileMetadata item from the schema file", () => {
            const [seed] = schemaSeeds(synth);
            const create = createCall(seed);
            const update = JSON.parse(SynthResult.flatten(seed.properties.Update));
            expect(update.parameters.Item).toEqual(create.parameters.Item);
            expect(update.parameters.ConditionExpression).toBe(
                "attribute_exists(metadataSchemaId) AND modifiedBy = :systemUser"
            );
            expect(create.physicalResourceId.id).toMatch(/_mirisstream_initialization$/);
            expect(seed.properties.Delete).toBeUndefined();

            const item = create.parameters.Item;
            expect(create.parameters.TableName).toMatch(/MetadataSchemaStorageTableV2/);
            expect(item.metadataSchemaId).toEqual({ S: SCHEMA_ID });
            expect(item["databaseId:metadataEntityType"]).toEqual({ S: "GLOBAL:fileMetadata" });
            expect(item.databaseId).toEqual({ S: "GLOBAL" });
            expect(item.metadataSchemaEntityType).toEqual({ S: "fileMetadata" });
            expect(item.schemaName).toEqual({ S: "Miris stream" });
            expect(item.fileKeyTypeRestriction).toEqual({
                S: mirisStreamSchema.fileKeyTypeRestriction,
            });
            expect(item.enabled).toEqual({ BOOL: true });
            expect(item.createdBy).toEqual({ S: "SYSTEM_USER" });
            expect(item.modifiedBy).toEqual({ S: "SYSTEM_USER" });
            expect(JSON.parse(item.fields.S)).toEqual({ fields: mirisStreamSchema.fields });
        });

        test("its provider runs on a role that can only put items into the schema table", () => {
            const [seed] = schemaSeeds(synth);
            const providers = synth.where(
                "AWS::Lambda::Function",
                (r) =>
                    r.stack === seed.stack &&
                    r.logicalId.startsWith("AWS679f53fac002430cb0da5b7982bd2287")
            );
            expect(providers).toHaveLength(1);
            const roleId = providers[0].properties.Role["Fn::GetAtt"][0];
            expect(roleId).toMatch(/^MirisStreamSchemaCustomResourceRole/);

            const [role] = synth.where(
                "AWS::IAM::Role",
                (r) => r.stack === seed.stack && r.logicalId === roleId
            );
            const statements = (role.properties.Policies as any[]).flatMap(
                (p) => p.PolicyDocument.Statement
            );
            expect(statements).toHaveLength(1);
            expect(statements[0].Action).toBe("dynamodb:PutItem");
            expect(SynthResult.flatten(statements[0].Resource)).toMatch(
                /MetadataSchemaStorageTableV2/
            );
            const attached = synth.where(
                "AWS::IAM::Policy",
                (p) =>
                    p.stack === seed.stack &&
                    (p.properties.Roles ?? []).some((r: any) => r?.Ref === roleId)
            );
            expect(attached).toEqual([]);
        });
    });
});

describe("Miris add-on enabled, viewerKeyMode perView, with the deployment CMK", () => {
    let synth: SynthResult;

    beforeAll(() => {
        synth = synthTemplate("commercial", {
            mutate: enableMiris("perView", true),
            mutateKey: "miris-addon-perview-kms",
        });
    });

    test("both routes are registered", () => {
        const paths = restApiPaths(synth);
        expect(paths[STATUS_ROUTE]).toBeDefined();
        expect(paths[VIEWER_KEY_ROUTE]).toBeDefined();
    });

    test("no Lambda carries a viewer key", () => {
        const withKey = synth
            .ofType("AWS::Lambda::Function")
            .filter((r) => r.properties.Environment?.Variables?.MIRIS_VIEWER_KEY !== undefined);
        expect(withKey.map((r) => r.logicalId)).toEqual([]);
        // Control: the Miris Lambdas exist and carry the mode.
        expect(mirisEnvironment(onlyFunction(synth, STATUS_HANDLER)).MIRIS_VIEWER_KEY_MODE).toBe(
            "perView"
        );
    });

    test("each Lambda carries exactly the Miris variables its handler reads", () => {
        const shared = {
            MIRIS_API_KEY_SECRET_ARN: SECRET_ARN,
            MIRIS_API_BASE_URL: "https://app.miris.com",
        };
        expect(mirisEnvironment(onlyFunction(synth, STATUS_HANDLER))).toEqual({
            MIRIS_VIEWER_KEY_MODE: "perView",
            ...shared,
        });
        expect(mirisEnvironment(onlyFunction(synth, MINT_HANDLER))).toEqual({
            MIRIS_VIEWER_KEY_MODE: "perView",
            MIRIS_VIEWER_KEY_TTL_MINUTES: "7",
            ...shared,
        });
        expect(mirisEnvironment(onlyFunction(synth, CLEANUP_HANDLER))).toEqual(shared);
    });

    test.each([STATUS_HANDLER, MINT_HANDLER, CLEANUP_HANDLER])(
        "%s reads exactly the configured secret",
        (handler) => {
            expectExactSecretGrant(roleStatements(synth, onlyFunction(synth, handler)));
        }
    );

    describe("viewer key table", () => {
        test("exists once, keyed by mirisViewerKeyId, in the add-on stack", () => {
            const tables = viewerKeyTables(synth);
            expect(tables).toHaveLength(1);
            expect(tables[0].logicalId).toMatch(/^MirisViewerKeyStorageTable/);
            expect(tables[0].stack).toMatch(/MirisBuilder/);
            expect(tables[0].properties.KeySchema).toEqual([
                { AttributeName: "mirisViewerKeyId", KeyType: "HASH" },
            ]);
            expect(tables[0].properties.TableName).toBeUndefined();
        });

        test("expires rows on ttl and streams their keys only", () => {
            const [table] = viewerKeyTables(synth);
            expect(table.properties.TimeToLiveSpecification).toEqual({
                AttributeName: "ttl",
                Enabled: true,
            });
            expect(table.properties.StreamSpecification).toEqual({ StreamViewType: "KEYS_ONLY" });
        });

        test("is encrypted with the deployment CMK, recoverable and retained", () => {
            const [table] = viewerKeyTables(synth);
            expect(table.properties.SSESpecification.SSEEnabled).toBe(true);
            expect(table.properties.SSESpecification.SSEType).toBe("KMS");
            expect(table.properties.SSESpecification.KMSMasterKeyId).toBeDefined();
            expect(
                table.properties.PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled
            ).toBe(true);
            expect(table.raw.DeletionPolicy).toBe("Retain");
        });

        test("publishes its name under the add-on resource-name key", () => {
            const params = viewerKeyParams(synth);
            expect(params).toHaveLength(1);
            expect(params[0].stack).toBe(viewerKeyTables(synth)[0].stack);
        });

        test("only the viewer key Lambda writes to it, and only PutItem", () => {
            const writers = synth
                .ofType("AWS::IAM::Policy")
                .flatMap((p) => p.properties.PolicyDocument.Statement as any[])
                .filter((s) => JSON.stringify(s.Resource).includes("MirisViewerKeyStorageTable"))
                .filter((s) => actionsOf(s).some((a) => WRITE_ACTION.test(a)));
            expect(writers).toHaveLength(1);
            expect(actionsOf(writers[0])).toEqual(["dynamodb:PutItem"]);

            const mintStatements = roleStatements(synth, onlyFunction(synth, MINT_HANDLER));
            expect(mintStatements).toContainEqual(writers[0]);
            const onTable = mintStatements.filter((s) =>
                JSON.stringify(s.Resource).includes("MirisViewerKeyStorageTable")
            );
            expect(onTable.flatMap(actionsOf)).toEqual(["dynamodb:PutItem"]);
        });

        test("the status Lambda neither reads nor writes it", () => {
            const statements = roleStatements(synth, onlyFunction(synth, STATUS_HANDLER));
            expect(JSON.stringify(statements)).not.toContain("MirisViewerKeyStorageTable");
            expect(statements.flatMap(actionsOf).filter((a) => WRITE_ACTION.test(a))).toEqual([]);
        });

        test("the cleanup Lambda reads its stream and writes no DynamoDB data", () => {
            const statements = roleStatements(synth, onlyFunction(synth, CLEANUP_HANDLER));
            const streamStatement = statements.find((s) =>
                actionsOf(s).includes("dynamodb:GetRecords")
            );
            expect(JSON.stringify(streamStatement.Resource)).toContain(
                "MirisViewerKeyStorageTable"
            );
            expect(statements.flatMap(actionsOf).filter((a) => WRITE_ACTION.test(a))).toEqual([]);
        });
    });

    describe("cleanup event source mapping", () => {
        test("maps the table's stream to the cleanup Lambda once", () => {
            const mappings = cleanupMappings(synth);
            expect(mappings).toHaveLength(1);
            expect(JSON.stringify(mappings[0].properties.EventSourceArn)).toContain(
                "MirisViewerKeyStorageTable"
            );
            expect(mappings[0].properties.StartingPosition).toBe("TRIM_HORIZON");
            expect(mappings[0].properties.BatchSize).toBe(5);
        });

        test("filters to TTL deletions only", () => {
            const [mapping] = cleanupMappings(synth);
            const filters = mapping.properties.FilterCriteria.Filters;
            expect(filters).toHaveLength(1);
            expect(JSON.parse(filters[0].Pattern)).toEqual({
                eventName: ["REMOVE"],
                userIdentity: {
                    type: ["Service"],
                    principalId: ["dynamodb.amazonaws.com"],
                },
            });
        });

        test("reports per-record failures, bisects, bounds retries, and has a CMK-encrypted DLQ", () => {
            const [mapping] = cleanupMappings(synth);
            expect(mapping.properties.FunctionResponseTypes).toEqual(["ReportBatchItemFailures"]);
            expect(mapping.properties.BisectBatchOnFunctionError).toBe(true);
            expect(mapping.properties.MaximumRetryAttempts).toBe(10);

            const destination = mapping.properties.DestinationConfig.OnFailure.Destination;
            const queueLogicalId = destination["Fn::GetAtt"][0];
            expect(queueLogicalId).toMatch(/^MirisViewerKeyCleanupDLQ/);
            const [queue] = synth.where(
                "AWS::SQS::Queue",
                (q) => q.stack === mapping.stack && q.logicalId === queueLogicalId
            );
            expect(queue.properties.KmsMasterKeyId).toBeDefined();
            expect(queue.properties.MessageRetentionPeriod).toBe(14 * 24 * 60 * 60);
        });
    });

    test("the storage stack takes no parameter from the add-on stacks", () => {
        // A grant that adds an add-on principal to the shared key's resource policy makes the
        // storage stack consume an add-on output, which CloudFormation rejects as circular.
        const storage = Object.values(synth.templates)
            .flatMap((t: any) => Object.entries<any>(t.Resources ?? {}))
            .filter(
                ([logicalId, r]) =>
                    r.Type === "AWS::CloudFormation::Stack" &&
                    logicalId.startsWith("StorageResourcesBuilder")
            );
        expect(storage).toHaveLength(1);
        const parameters = JSON.stringify(storage[0][1].Properties.Parameters ?? {});
        expect(parameters).not.toMatch(/AddonBuilder|MirisBuilder/);
    });
});
