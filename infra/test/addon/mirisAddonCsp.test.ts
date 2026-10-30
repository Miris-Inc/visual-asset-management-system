/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The web Content-Security-Policy sources the Miris add-on contributes.
 *
 * The Miris web SDK streams from `*.miris.com` over HTTPS and WebSocket, and loads part of its engine
 * through a dynamic `import()` of a `blob:` script URL, which `script-src` blocks unless the source is
 * listed. With `app.addons.useMirisStreaming` enabled the add-on adds those hosts to `connect-src`
 * and `blob:` to `script-src`. The Miris API base URL is called only by the add-on's Lambdas, so it
 * adds no browser source; with it off the policy is unchanged. `'unsafe-eval'`, which the SDK also needs, comes
 * from `app.webUi.allowUnsafeEvalFeatures`, which `getConfig()` requires with the add-on.
 *
 * The ALB carries the whole policy in one listener attribute capped at 1 KB, and the construct fails
 * synthesis above it, so the add-on is also synthesized on the ALB path.
 */

import * as fs from "fs";
import * as path from "path";
import * as Config from "../../config/config";
import * as Service from "../../lib/helper/service-helper";
import { generateContentSecurityPolicy } from "../../lib/helper/security";
import { addonCspSources } from "../../lib/nestedStacks/addon/addonCsp";
import { SynthResult, synthTemplate } from "../support/templateSynth";

const SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf";

/** The ALB listener attribute cap, and its measurement of a token as resolved text. */
const ALB_LISTENER_ATTRIBUTE_MAX_BYTES = 1024;
const deployedBytes = (csp: string) =>
    Buffer.byteLength(csp.replace(/\$\{[^}]*\}/g, "x".repeat(10)), "utf8");

function enableMiris(c: any, mirisApiBaseUrl = "https://app.miris.com") {
    c.app.webUi.allowUnsafeEvalFeatures = true;
    const miris = c.app.addons.useMirisStreaming;
    miris.enabled = true;
    miris.viewerKeyMode = "static";
    miris.viewerKey = "test-placeholder-viewer-key-000000";
    miris.apiKeySecretArn = SECRET_ARN;
    miris.mirisApiBaseUrl = mirisApiBaseUrl;
}

function commercialConfig(mutate?: (c: any) => void): Config.Config {
    const config = JSON.parse(
        fs.readFileSync(
            path.join(__dirname, "..", "..", "config", "config.template.commercial.json"),
            "utf8"
        )
    );
    config.env.account = "123456789012";
    config.env.region = "us-east-1";
    config.env.partition = "aws";
    mutate?.(config);
    return config as Config.Config;
}

function policy(config: Config.Config): string {
    Service.SetConfig(config);
    return generateContentSecurityPolicy(
        {} as any,
        "https://auth.example.com",
        "api.example.com",
        config
    );
}

function directive(csp: string, name: string): string[] {
    const found = csp
        .split(";")
        .map((part) => part.trim())
        .find((part) => part.startsWith(`${name} `));
    if (!found) throw new Error(`no ${name} directive in: ${csp}`);
    return found.split(/\s+/).slice(1);
}

describe("Miris add-on CSP sources", () => {
    test("the add-on contributes nothing when it is off", () => {
        expect(addonCspSources(commercialConfig())).toEqual({
            connectSrc: [],
            frameSrc: [],
            scriptSrc: [],
        });
    });

    test("the add-on contributes the Miris hosts and blob: scripts", () => {
        expect(addonCspSources(commercialConfig((c) => enableMiris(c)))).toEqual({
            connectSrc: ["https://*.miris.com", "wss://*.miris.com"],
            frameSrc: [],
            scriptSrc: ["blob:"],
        });
    });

    test("the Miris API base URL adds no browser source", () => {
        const sources = addonCspSources(
            commercialConfig((c) => enableMiris(c, "https://miris.staging.example.com/api/"))
        );
        expect(sources.connectSrc).toEqual(["https://*.miris.com", "wss://*.miris.com"]);
    });

    test("the merged policy changes only connect-src and script-src", () => {
        const off = policy(commercialConfig((c) => (c.app.webUi.allowUnsafeEvalFeatures = true)));
        const on = policy(commercialConfig((c) => enableMiris(c)));
        const added = (name: string) =>
            directive(on, name).filter((source) => !directive(off, name).includes(source));
        expect(added("connect-src")).toEqual(["https://*.miris.com", "wss://*.miris.com"]);
        expect(added("script-src")).toEqual(["blob:"]);
        for (const name of ["worker-src", "img-src", "media-src", "frame-src", "font-src"]) {
            expect(directive(on, name)).toEqual(directive(off, name));
        }
        // Control: the script-src the add-on extends is the real one.
        expect(directive(on, "script-src")).toContain("'unsafe-eval'");
    });
});

describe("Miris add-on on the ALB path", () => {
    let synth: SynthResult;

    beforeAll(() => {
        synth = synthTemplate("commercial", {
            mutate: (c: any) => {
                enableMiris(c);
                c.app.useCloudFront.enabled = false;
                c.app.useGlobalVpc.enabled = true;
                c.app.useAlb.enabled = true;
                c.app.useAlb.domainHost = "vams-t1.example.com";
                c.app.useAlb.certificateArn =
                    "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555";
                c.app.useAlb.optionalHostedZoneId = "";
            },
            mutateKey: "miris-addon-alb",
        });
    });

    test("the listener policy carries the Miris sources within the attribute cap", () => {
        const values = synth
            .ofType("AWS::ElasticLoadBalancingV2::Listener")
            .flatMap((r) => r.properties.ListenerAttributes ?? [])
            .filter(
                (a: any) => a.Key === "routing.http.response.content_security_policy.header_value"
            )
            .map((a: any) => SynthResult.flatten(a.Value));
        expect(values).toHaveLength(1);
        const csp = values[0];
        expect(directive(csp, "connect-src")).toEqual(
            expect.arrayContaining(["https://*.miris.com", "wss://*.miris.com"])
        );
        expect(directive(csp, "script-src")).toContain("blob:");
        expect(deployedBytes(csp)).toBeLessThanOrEqual(ALB_LISTENER_ATTRIBUTE_MAX_BYTES);
    });
});
