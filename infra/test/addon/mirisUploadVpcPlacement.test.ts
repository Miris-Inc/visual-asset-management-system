/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Miris upload pipeline runs its Fargate jobs in the PRIVATE subnets, which reach Miris over the
 * NAT gateway, and needs no ECS control-plane endpoint.
 *
 * `addonBuilder-nestedStack.ts` passes the pipeline `pipelineNetwork.privateSubnets.pipeline`, so the
 * VPC builder must create the public and private subnets (with their NAT gateways) for it, and the
 * Batch and ECR endpoints its tasks pull their image through. The ECS endpoint serves the agent of an
 * EC2-launch-type container instance; Fargate tasks do not use it, so it is not created for this flag.
 *
 * Synthesized from the shipped commercial template with the Miris add-on and its upload pipeline
 * enabled, and no core pipeline. The pipeline's nested stack is a child of the add-on stack, and the
 * add-on publishes the `MIRIS_UPLOAD` switch with it.
 */

import { SynthResult, synthTemplate } from "../support/templateSynth";

const SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf";
const PLACEHOLDER_VIEWER_KEY = "test-placeholder-viewer-key-000000";

/**
 * Enable the Miris add-on and its upload pipeline, and no other feature that would create public
 * subnets.
 */
function onlyMirisUpload(c: any) {
    c.app.useGlobalVpc.enabled = true;
    c.app.useGlobalVpc.addVpcEndpoints = true;
    c.app.useAlb.enabled = false;
    c.app.useCloudFront.enabled = true;
    for (const name of Object.keys(c.app.pipelines)) {
        const entry = c.app.pipelines[name];
        if (entry && typeof entry === "object" && "enabled" in entry) {
            entry.enabled = false;
            if (entry.autoRegisterWithVAMS !== undefined) {
                entry.autoRegisterWithVAMS = false;
            }
        }
    }
    if (c.app.pipelines.useRapidPipeline) {
        c.app.pipelines.useRapidPipeline.useEcs.enabled = false;
        c.app.pipelines.useRapidPipeline.useEks.enabled = false;
    }
    c.app.webUi.allowUnsafeEvalFeatures = true;
    const miris = c.app.addons.useMirisStreaming;
    miris.enabled = true;
    miris.viewerKey = PLACEHOLDER_VIEWER_KEY;
    miris.apiKeySecretArn = SECRET_ARN;
    miris.upload.enabled = true;
    miris.upload.autoRegisterWithVAMS = false;
}

/** True when the deployment records `feature` in its enabled-features table. */
function recordsFeature(synth: SynthResult, feature: string): boolean {
    return synth
        .ofType("Custom::AWS")
        .some((r) => JSON.stringify(r.properties.Create).includes(`-${feature}\\"`));
}

const endpointServices = (synth: SynthResult): string[] =>
    synth
        .ofType("AWS::EC2::VPCEndpoint")
        .map((e) => SynthResult.flatten((e.properties as any).ServiceName));

describe("the Miris upload pipeline runs Fargate in private subnets", () => {
    let synth: SynthResult;

    beforeAll(() => {
        synth = synthTemplate("commercial", {
            mutate: onlyMirisUpload,
            mutateKey: "vpc-only-miris-upload",
        });
    });

    test("[control] the pipeline's own nested stack is in this synth", () => {
        const jobDefinitions = synth
            .ofType("AWS::Batch::JobDefinition")
            .filter((jd) =>
                /MirisUploadJob_/.test(JSON.stringify(jd.properties.JobDefinitionName))
            );
        expect(jobDefinitions).toHaveLength(1);
        expect(new Set(jobDefinitions.map((jd) => jd.stack)).size).toBe(1);
        expect(jobDefinitions[0].stack).toMatch(/AddonBuilder.*MirisUploadBuilderNestedStack/);
    });

    test("the add-on publishes the MIRIS_UPLOAD switch with the pipeline", () => {
        expect(recordsFeature(synth, "MIRIS_UPLOAD")).toBe(true);
        expect(recordsFeature(synth, "MIRIS_ADDON")).toBe(true);
    });

    test("the pipeline's VAMS execute Lambda is a stack output", () => {
        const templates = synth.templates as Record<
            string,
            { Outputs?: Record<string, { Value: unknown }> }
        >;
        const outputs = Object.values(templates)
            .flatMap((tpl) => Object.entries(tpl.Outputs ?? {}))
            .filter(([id]) => id.startsWith("VamsPipelineExecuteLambdaFunctionOutput"));
        // The commercial template enables no core pipeline, so the one output is the add-on's.
        expect(outputs).toHaveLength(1);
        expect(outputs.map(([, output]) => JSON.stringify(output.Value))).toContainEqual(
            expect.stringMatching(/AddonBuilder/)
        );
    });

    test("its Fargate compute environment is placed in the private subnets", () => {
        const environments = synth
            .ofType("AWS::Batch::ComputeEnvironment")
            .filter((e) => /FARGATE/i.test(JSON.stringify((e.properties as any).ComputeResources)));
        expect(environments).toHaveLength(1);
        // Subnets cross into the nested stack as parameters named after the VPC builder's subnets.
        const subnets = JSON.stringify(environments[0].properties.ComputeResources.Subnets);
        expect(subnets).toMatch(/[Pp]rivate/);
        expect(subnets).not.toMatch(/[Ii]solated/);
    });

    test("NAT gateways and public subnets are created", () => {
        expect(synth.countOfType("AWS::EC2::NatGateway")).toBeGreaterThan(0);
        const publicSubnets = synth.ofType("AWS::EC2::Subnet").filter((s) => {
            const tags = ((s.properties as any).Tags ?? []) as Array<{
                Key: string;
                Value: unknown;
            }>;
            return tags.some(
                (t) => t.Key === "aws-cdk:subnet-type" && String(t.Value) === "Public"
            );
        });
        expect(publicSubnets.length).toBeGreaterThan(0);
    });

    test("the Batch and ECR endpoints its tasks need are created", () => {
        const services = endpointServices(synth);
        expect(services.some((s) => /\.batch$/.test(s))).toBe(true);
        expect(services.some((s) => /\.ecr\.api$/.test(s))).toBe(true);
        expect(services.some((s) => /\.ecr\.dkr$/.test(s))).toBe(true);
    });

    test("no ECS control-plane endpoint is created for it", () => {
        expect(endpointServices(synth).filter((s) => /\.ecs$/.test(s))).toEqual([]);
    });
});
