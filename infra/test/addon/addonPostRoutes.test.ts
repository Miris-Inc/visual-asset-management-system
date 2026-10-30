/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The add-on lane of the basic read-only role's POST constraint.
 *
 * `addonReadOnlyApiPostRouteCriteria()` (`lib/nestedStacks/addon/addonDefaultRoutes.ts`) returns the
 * `route__path` criteria the enabled add-ons add to `initial_basicro_allow_post_apis`, for add-on routes
 * that read through a POST body. `DynamoDbAuthDefaultsROConstructStack` splices them at the end of that
 * constraint's `criteriaOr`, after the core `/auth/loginProfile` criterion.
 *
 * Each seeded constraint is written by a custom resource whose id and physical id are numbered by its
 * position in the construct's constraint list, so a criterion appended inside an existing constraint
 * must leave every custom resource's id, and every other constraint's item, exactly as they were.
 *
 * A stand-in criterion is injected by replacing the function, so the lane is tested apart from any
 * shipped add-on's criteria. The construct is synthesized on its own: its constraint items are
 * config-independent apart from the add-on criteria.
 */

import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as Config from "../../config/config";
import * as AddonDefaultRoutes from "../../lib/nestedStacks/addon/addonDefaultRoutes";
import { DynamoDbAuthDefaultsROConstructStack } from "../../lib/nestedStacks/auth/constructs/dynamodb-authdefaults-ro-construct";
import { newTestApp } from "../support/testApp";

const TEMPLATE_NAMES = ["commercial", "govcloud", "eusovereign"];

function templateConfig(name: string, mutate?: (c: any) => void): Config.Config {
    const config = JSON.parse(
        fs.readFileSync(
            path.join(__dirname, "..", "..", "config", `config.template.${name}.json`),
            "utf8"
        )
    );
    mutate?.(config);
    return config as Config.Config;
}

interface ConstraintItem {
    /** Custom resource logical id. */
    logicalId: string;
    physicalResourceId: string;
    constraintId: string;
    criteriaOr: { field: string; id: string; operator: string; value: string }[];
    groupPermissions: { permission: string; permissionType: string }[];
    /** The whole item with its creation timestamps removed, for comparing two synths. */
    stable: Record<string, unknown>;
}

/** `Fn::Join` of literal strings and table `Ref`s, flattened with each `Ref` named. */
function flatten(value: any): string {
    if (typeof value === "string") return value;
    if (value?.["Fn::Join"]) {
        const [separator, parts] = value["Fn::Join"];
        return parts.map(flatten).join(separator);
    }
    if (value?.Ref) return `<${value.Ref}>`;
    throw new Error(`unexpected token in custom resource payload: ${JSON.stringify(value)}`);
}

/** Synthesizes the construct and returns its constraint items in custom resource order. */
function synthConstraintItems(config: Config.Config): ConstraintItem[] {
    const app = newTestApp();
    const stack = new cdk.Stack(app, "AuthDefaultsRo", {
        env: { account: "123456789012", region: "us-east-1" },
    });
    const table = (id: string) =>
        new dynamodb.Table(stack, id, {
            partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
        });
    new DynamoDbAuthDefaultsROConstructStack(stack, "AuthDefaultsRo", {
        config,
        customResourceRole: new iam.Role(stack, "CustomResourceRole", {
            assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
        }),
        lambdaCommonBaseLayer: undefined as any,
        storageResources: {
            dynamo: {
                rolesStorageTable: table("RolesStorageTable"),
                constraintsStorageTable: table("ConstraintsStorageTable"),
            },
        } as any,
    });

    const resources = app.synth().getStackByName("AuthDefaultsRo").template.Resources;
    return Object.entries<any>(resources)
        .filter(([logicalId]) => logicalId.includes("constraintsStorageTablebasicroCustomResource"))
        .map(([logicalId, resource]) => {
            const create = JSON.parse(flatten(resource.Properties.Create));
            const item = create.parameters.Item;
            const stable = { ...item };
            delete stable.dateCreated;
            delete stable.dateModified;
            return {
                logicalId,
                physicalResourceId: create.physicalResourceId.id,
                constraintId: item.constraintId.S,
                criteriaOr: JSON.parse(item.criteriaOr.S),
                groupPermissions: JSON.parse(item.groupPermissions.S),
                stable,
            };
        });
}

const POST_CONSTRAINT = "initial_basicro_allow_post_apis#group#basicReadOnly";
const GET_CONSTRAINT = "initial_basicro_allow_get_apis#group#basicReadOnly";

const CORE_POST_CRITERIA = [
    "0_basicro_api_paths_post",
    "1_basicro_api_paths_post",
    "2_basicro_api_paths_post",
    "3_basicro_api_paths_post",
];

const STAND_IN_CRITERION = {
    M: {
        field: { S: "route__path" },
        id: { S: "90_basicro_api_paths_post" },
        operator: { S: "starts_with" },
        value: { S: "/addon/standin/lookup" },
    },
};

const byConstraint = (items: ConstraintItem[], constraintId: string): ConstraintItem => {
    const found = items.filter((item) => item.constraintId === constraintId);
    expect(found).toHaveLength(1);
    return found[0];
};

describe("read-only POST constraint add-on lane", () => {
    afterEach(() => {
        jest.restoreAllMocks();
    });

    it.each(TEMPLATE_NAMES)("contributes no criterion from the %s template", (name) => {
        expect(
            AddonDefaultRoutes.addonReadOnlyApiPostRouteCriteria(templateConfig(name), "basicro")
        ).toEqual([]);
    });

    it("contributes no criterion with the Physna add-on enabled", () => {
        const config = templateConfig("commercial", (c) => {
            c.app.addons.usePhysnaSync.enabled = true;
        });
        expect(AddonDefaultRoutes.addonReadOnlyApiPostRouteCriteria(config, "basicro")).toEqual([]);
    });

    it("seeds only the core POST criteria when no add-on contributes one", () => {
        const items = synthConstraintItems(templateConfig("commercial"));
        expect(byConstraint(items, POST_CONSTRAINT).criteriaOr.map((c) => c.id)).toEqual(
            CORE_POST_CRITERIA
        );
    });

    describe("with a stand-in add-on criterion", () => {
        const config = templateConfig("commercial");
        let baseline: ConstraintItem[];
        let injected: ConstraintItem[];

        beforeAll(() => {
            baseline = synthConstraintItems(config);
            const spy = jest
                .spyOn(AddonDefaultRoutes, "addonReadOnlyApiPostRouteCriteria")
                .mockReturnValue([STAND_IN_CRITERION]);
            injected = synthConstraintItems(config);
            expect(spy).toHaveBeenCalledWith(config, "basicro");
            spy.mockRestore();
        });

        it("appends it to the POST constraint after the core criteria", () => {
            const post = byConstraint(injected, POST_CONSTRAINT);
            expect(post.criteriaOr.map((c) => c.id)).toEqual([
                ...CORE_POST_CRITERIA,
                "90_basicro_api_paths_post",
            ]);
            expect(post.criteriaOr[post.criteriaOr.length - 1]).toEqual({
                field: "route__path",
                id: "90_basicro_api_paths_post",
                operator: "starts_with",
                value: "/addon/standin/lookup",
            });
        });

        it("grants it for POST only", () => {
            expect(byConstraint(injected, POST_CONSTRAINT).groupPermissions).toEqual([
                expect.objectContaining({ permission: "POST", permissionType: "allow" }),
            ]);
            const elsewhere = injected.filter((item) =>
                item.criteriaOr.some((c) => c.value === "/addon/standin/lookup")
            );
            expect(elsewhere.map((item) => item.constraintId)).toEqual([POST_CONSTRAINT]);
            expect(
                byConstraint(injected, GET_CONSTRAINT).criteriaOr.map((c) => c.value)
            ).not.toContain("/addon/standin/lookup");
        });

        it("keeps every custom resource id and physical id", () => {
            const ids = (items: ConstraintItem[]) =>
                items.map((item) => [item.logicalId, item.physicalResourceId, item.constraintId]);
            expect(injected.length).toBeGreaterThan(0);
            expect(ids(injected)).toEqual(ids(baseline));
        });

        it("leaves every other constraint item unchanged", () => {
            const others = (items: ConstraintItem[]) =>
                items.filter((item) => item.constraintId !== POST_CONSTRAINT).map((i) => i.stable);
            expect(others(injected)).toEqual(others(baseline));
            // Control: the POST item itself did change, so the comparison above can see a change.
            expect(byConstraint(injected, POST_CONSTRAINT).stable).not.toEqual(
                byConstraint(baseline, POST_CONSTRAINT).stable
            );
        });
    });

    it("appends the Physna GET criterion after the core GET criteria", () => {
        const config = templateConfig("commercial", (c) => {
            c.app.addons.usePhysnaSync.enabled = true;
        });
        const physna = AddonDefaultRoutes.addonReadOnlyApiRouteCriteria(config, "basicro");
        expect(physna).toHaveLength(1);
        const get = byConstraint(synthConstraintItems(config), GET_CONSTRAINT);
        expect(get.criteriaOr[get.criteriaOr.length - 1]).toEqual({
            field: "route__path",
            id: "16_basicro_api_paths",
            operator: "starts_with",
            value: "/addon/physna/viewer",
        });
    });
});
