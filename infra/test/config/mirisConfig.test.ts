/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `getConfig()` rules for the Miris Spatial Streaming add-on (`app.addons.useMirisStreaming`):
 * defaults, the partition gate, the unsafe-eval requirement, the viewer key mode and lifetime, the
 * viewer key and Integration Key secret ARN formats, and the outbound check on `mirisApiBaseUrl`.
 *
 * Every rejection asserts on the MESSAGE. A Miris config can be invalid in several ways at once, so a
 * bare `.toThrow()` would pass while the rule under test did nothing. The ConfigBuilder's hand-ported
 * rules are asserted from the same cases, because `configBuilderSync.test.ts` covers only `schema.ts`
 * and `defaults.ts`.
 */

import * as fs from "fs";
import * as Config from "../../config/config";
import commercialTemplate from "../../config/config.template.commercial.json";
import eusovereignTemplate from "../../config/config.template.eusovereign.json";
import govcloudTemplate from "../../config/config.template.govcloud.json";
import { newTestApp } from "../support/testApp";
import { makeDefaultConfig } from "../../../documentation/docusaurus-site/src/components/ConfigBuilder/defaults";
import { evaluateRules } from "../../../documentation/docusaurus-site/src/components/ConfigBuilder/validation";

const realReadFileSync = jest.requireActual("fs").readFileSync;

jest.mock("fs", () => {
    const actual = jest.requireActual("fs");
    return { ...actual, readFileSync: jest.fn(actual.readFileSync) };
});

const SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-miris-api-key-AbCdEf";
const VIEWER_KEY = "test-placeholder-viewer-key-000000";

const TEMPLATES = {
    commercial: { template: commercialTemplate, region: "us-east-1" },
    govcloud: { template: govcloudTemplate, region: "us-gov-west-1" },
    eusovereign: { template: eusovereignTemplate, region: "eusc-de-east-1" },
} as const;

/** A config built from a template with the placeholders getConfig() requires filled. */
function templateFor(name: keyof typeof TEMPLATES): any {
    const { template, region } = TEMPLATES[name];
    const config = JSON.parse(JSON.stringify(template));
    config.env.region = region;
    config.env.account = "123456789012";
    config.app.baseStackName = "vamstest";
    if (config.app.useAlb?.enabled) {
        config.app.useAlb.domainHost = "vams.example.com";
        config.app.useAlb.certificateArn =
            "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555";
    }
    return config;
}

/** Serves a template with `mutate` applied as config.json and returns a thunk calling getConfig(). */
function resolve(
    mutate: (c: any) => void,
    template: keyof typeof TEMPLATES = "commercial"
): () => Config.Config {
    const config = templateFor(template);
    mutate(config);
    (fs.readFileSync as unknown as jest.Mock).mockImplementation(
        (p: string, ...rest: unknown[]) => {
            if (typeof p === "string" && p.endsWith("config.json")) return JSON.stringify(config);
            return realReadFileSync(p, ...rest);
        }
    );
    return () => Config.getConfig(newTestApp());
}

/** The ConfigBuilder rule ids with `prefix` reported for the same config getConfig() is given. */
function builderRules(
    mutate: (c: any) => void,
    prefix: string,
    template: keyof typeof TEMPLATES = "commercial"
): string[] {
    const config = templateFor(template);
    mutate(config);
    return evaluateRules(config)
        .map((rule) => rule.id)
        .filter((id) => id.startsWith(prefix));
}

/** Static mode: the deployment viewer key, plus the Integration Key the tag check reads. */
function enableStatic(c: any) {
    c.app.webUi.allowUnsafeEvalFeatures = true;
    const miris = c.app.addons.useMirisStreaming;
    miris.enabled = true;
    miris.viewerKeyMode = "static";
    miris.viewerKey = VIEWER_KEY;
    miris.apiKeySecretArn = SECRET_ARN;
}

function enablePerView(c: any) {
    enableStatic(c);
    c.app.addons.useMirisStreaming.viewerKeyMode = "perView";
    c.app.addons.useMirisStreaming.viewerKey = "";
}

/** Everything a restricted-partition template needs to pass getConfig() before the Miris rules. */
function restrictedPartitionDeployable(c: any) {
    c.app.useGlobalVpc.enabled = true;
}

let warn: jest.SpyInstance;

beforeEach(() => {
    warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
    warn.mockRestore();
    // Restored to delegating rather than cleared, or a later test in this process reads nothing.
    (fs.readFileSync as unknown as jest.Mock).mockImplementation(realReadFileSync);
});

function mirisWarnings(): string[] {
    return warn.mock.calls.map((args) => String(args[0])).filter((w) => w.includes("Miris"));
}

describe("shipped templates", () => {
    test.each(Object.keys(TEMPLATES) as (keyof typeof TEMPLATES)[])(
        "the %s template passes getConfig() with the Miris add-on disabled",
        (name) => {
            const config = resolve(() => undefined, name)();
            expect(config.app.addons.useMirisStreaming).toEqual({
                enabled: false,
                viewerKeyMode: "static",
                viewerKey: "",
                viewerKeyTtlMinutes: 15,
                apiKeySecretArn: "",
                mirisApiBaseUrl: "https://app.miris.com",
            });
        }
    );

    test.each(["commercial", "govcloud", "eusovereign"] as const)(
        "the %s ConfigBuilder preset reports no Miris rule",
        (profile) => {
            const ids = evaluateRules(makeDefaultConfig(profile)).map((rule) => rule.id);
            expect(ids.filter((id) => id.startsWith("miris-"))).toEqual([]);
        }
    );
});

describe("defaults", () => {
    test("a config without the add-on block gets every default", () => {
        const config = resolve((c) => {
            delete c.app.addons.useMirisStreaming;
        })();
        expect(config.app.addons.useMirisStreaming.viewerKeyMode).toBe("static");
        expect(config.app.addons.useMirisStreaming.viewerKeyTtlMinutes).toBe(15);
        expect(config.app.addons.useMirisStreaming.mirisApiBaseUrl).toBe("https://app.miris.com");
    });

    test("each missing field is filled on its own", () => {
        const config = resolve((c) => {
            c.app.addons.useMirisStreaming = {
                enabled: false,
                viewerKeyTtlMinutes: 30,
            };
        })();
        expect(config.app.addons.useMirisStreaming).toEqual({
            enabled: false,
            viewerKeyMode: "static",
            viewerKey: "",
            viewerKeyTtlMinutes: 30,
            apiKeySecretArn: "",
            mirisApiBaseUrl: "https://app.miris.com",
        });
    });

    test("a disabled add-on with stale invalid fields is not validated", () => {
        const mutate = (c: any) => {
            c.app.addons.useMirisStreaming = {
                enabled: false,
                viewerKeyMode: "dynamic",
                viewerKey: "short",
                viewerKeyTtlMinutes: 0,
                apiKeySecretArn: "not-an-arn",
                mirisApiBaseUrl: "http://169.254.169.254",
            };
        };
        expect(resolve(mutate)).not.toThrow();
        expect(mirisWarnings()).toEqual([]);
        expect(builderRules(mutate, "miris-")).toEqual([]);
    });
});

describe("Miris Spatial Streaming add-on", () => {
    test("static mode with a viewer key and an Integration Key secret is accepted", () => {
        const config = resolve(enableStatic)();
        expect(config.app.addons.useMirisStreaming.viewerKeyMode).toBe("static");
        expect(config.app.addons.useMirisStreaming.viewerKey).toBe(VIEWER_KEY);
        expect(mirisWarnings()).toEqual([]);
        expect(builderRules(enableStatic, "miris-")).toEqual([]);
    });

    test("perView mode with an Integration Key secret and no viewer key is accepted", () => {
        const config = resolve(enablePerView)();
        expect(config.app.addons.useMirisStreaming.viewerKeyMode).toBe("perView");
        expect(mirisWarnings()).toEqual([]);
        expect(builderRules(enablePerView, "miris-")).toEqual([]);
    });

    test("perView mode warns that a configured viewer key is ignored", () => {
        const mutate = (c: any) => {
            enablePerView(c);
            c.app.addons.useMirisStreaming.viewerKey = VIEWER_KEY;
        };
        expect(resolve(mutate)().app.addons.useMirisStreaming.viewerKeyMode).toBe("perView");
        expect(mirisWarnings()).toContainEqual(
            expect.stringMatching(/viewerKey is ignored when viewerKeyMode is "perView"/)
        );
        expect(builderRules(mutate, "miris-")).toEqual(["miris-perview-viewerkey-ignored-warn"]);
    });

    describe("partition", () => {
        test.each(["govcloud", "eusovereign"] as const)(
            "the %s partition is rejected",
            (template) => {
                const mutate = (c: any) => {
                    restrictedPartitionDeployable(c);
                    enableStatic(c);
                };
                expect(resolve(mutate, template)).toThrow(
                    /addons\.useMirisStreaming is enabled while deploying to the 'aws-(us-gov|eusc)' partition/
                );
                expect(builderRules(mutate, "miris-", template)).toEqual([
                    "miris-commercial-partition-only",
                ]);
            }
        );

        test("[control] the same restricted template deploys with the add-on disabled", () => {
            expect(resolve(restrictedPartitionDeployable, "govcloud")).not.toThrow();
            expect(builderRules(restrictedPartitionDeployable, "miris-", "govcloud")).toEqual([]);
        });
    });

    test("requires allowUnsafeEvalFeatures", () => {
        const mutate = (c: any) => {
            enableStatic(c);
            c.app.webUi.allowUnsafeEvalFeatures = false;
        };
        expect(resolve(mutate)).toThrow(
            /addons\.useMirisStreaming requires app\.webUi\.allowUnsafeEvalFeatures = true/
        );
        expect(builderRules(mutate, "miris-")).toEqual(["miris-unsafe-eval"]);
    });

    describe("viewerKeyMode", () => {
        test.each(["dynamic", "perview", "Static"])("%p is rejected", (mode) => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.viewerKeyMode = mode;
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.viewerKeyMode must be one of "static", "perView"/
            );
            expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkeymode"]);
        });
    });

    describe("viewerKeyTtlMinutes", () => {
        test.each([1, 15, 60])("%p is accepted", (ttl) => {
            const mutate = (c: any) => {
                enablePerView(c);
                c.app.addons.useMirisStreaming.viewerKeyTtlMinutes = ttl;
            };
            expect(resolve(mutate)().app.addons.useMirisStreaming.viewerKeyTtlMinutes).toBe(ttl);
            expect(builderRules(mutate, "miris-")).toEqual([]);
        });

        test.each([0, 61, -5, 15.5, "15", true])("%p is rejected", (ttl) => {
            const mutate = (c: any) => {
                enablePerView(c);
                c.app.addons.useMirisStreaming.viewerKeyTtlMinutes = ttl;
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.viewerKeyTtlMinutes must be a whole number of minutes from 1 to 60/
            );
            expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkey-ttl"]);
        });

        test("a null value is treated as absent and gets 15", () => {
            const mutate = (c: any) => {
                enablePerView(c);
                c.app.addons.useMirisStreaming.viewerKeyTtlMinutes = null;
            };
            expect(resolve(mutate)().app.addons.useMirisStreaming.viewerKeyTtlMinutes).toBe(15);
            expect(builderRules(mutate, "miris-")).toEqual([]);
        });
    });

    describe("apiKeySecretArn", () => {
        test.each([
            ["static", enableStatic],
            ["perView", enablePerView],
        ])("is required in %s mode", (_mode, enable) => {
            for (const value of ["", "UNDEFINED"]) {
                const mutate = (c: any) => {
                    enable(c);
                    c.app.addons.useMirisStreaming.apiKeySecretArn = value;
                };
                expect(resolve(mutate)).toThrow(
                    /addons\.useMirisStreaming requires apiKeySecretArn, the Secrets Manager ARN of the Miris Integration Key/
                );
                expect(builderRules(mutate, "miris-")).toEqual(["miris-secret-required"]);
            }
        });

        test.each([
            SECRET_ARN,
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:miris/integration_key-0a1B2c",
        ])("%p is accepted in both modes", (arn) => {
            for (const enable of [enableStatic, enablePerView]) {
                const mutate = (c: any) => {
                    enable(c);
                    c.app.addons.useMirisStreaming.apiKeySecretArn = arn;
                };
                expect(resolve(mutate)().app.addons.useMirisStreaming.apiKeySecretArn).toBe(arn);
                expect(builderRules(mutate, "miris-")).toEqual([]);
            }
        });

        test.each([
            // A partial ARN, without the suffix Secrets Manager appends, does not match the IAM grant.
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:miris-integration-key",
            "miris-integration-key",
            "arn:aws:secretsmanager:us-east-1:123456789012:miris-integration-key-AbCdEf",
            "arn:aws:s3:::miris-integration-key-AbCdEf",
            "arn:aws:secretsmanager:us-east-1:12345:secret:miris-integration-key-AbCdEf",
            // The add-on deploys to the commercial partition only, so only an `aws` ARN can name its secret.
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:miris/integration_key-AbCdEf",
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:miris-key-0a1B2c",
            "arn:awsfoo:secretsmanager:us-east-1:123456789012:secret:miris-integration-key-AbCdEf",
            "arn:aws-:secretsmanager:us-east-1:123456789012:secret:miris-integration-key-AbCdEf",
        ])("%p is rejected in both modes", (arn) => {
            for (const enable of [enableStatic, enablePerView]) {
                const mutate = (c: any) => {
                    enable(c);
                    c.app.addons.useMirisStreaming.apiKeySecretArn = arn;
                };
                expect(resolve(mutate)).toThrow(
                    /addons\.useMirisStreaming\.apiKeySecretArn must be the complete ARN of a Secrets Manager secret/
                );
                expect(builderRules(mutate, "miris-")).toEqual(["miris-secret-arn-format"]);
            }
        });
    });

    describe("viewerKey in static mode", () => {
        test.each(["", "UNDEFINED", "   ", "\t"])(
            "an unset or blank viewer key %p is rejected",
            (value) => {
                const mutate = (c: any) => {
                    enableStatic(c);
                    c.app.addons.useMirisStreaming.viewerKey = value;
                };
                expect(resolve(mutate)).toThrow(
                    /addons\.useMirisStreaming\.viewerKeyMode "static" requires viewerKey/
                );
                expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkey-required"]);
            }
        );

        test.each([` ${VIEWER_KEY}`, `${VIEWER_KEY} `, `${VIEWER_KEY}\n`, " short "])(
            "a viewer key with surrounding whitespace %p is rejected rather than stored",
            (value) => {
                const mutate = (c: any) => {
                    enableStatic(c);
                    c.app.addons.useMirisStreaming.viewerKey = value;
                };
                expect(resolve(mutate)).toThrow(
                    /addons\.useMirisStreaming\.viewerKey has leading or trailing whitespace/
                );
                expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkey-whitespace"]);
            }
        );

        test("a viewer key shorter than 16 characters is rejected with its own message", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.viewerKey = "too-short";
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.viewerKey is 9 characters long; a Miris viewer key is at least 16/
            );
            expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkey-short"]);
        });

        test("a viewer key of exactly 16 characters is accepted", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.viewerKey = "0123456789abcdef";
            };
            expect(resolve(mutate)().app.addons.useMirisStreaming.viewerKey).toBe(
                "0123456789abcdef"
            );
            expect(builderRules(mutate, "miris-")).toEqual([]);
        });

        test("an absent mode is static and still requires the viewer key", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                delete c.app.addons.useMirisStreaming.viewerKeyMode;
                c.app.addons.useMirisStreaming.viewerKey = "";
            };
            expect(resolve(mutate)).toThrow(/"static" requires viewerKey/);
            expect(builderRules(mutate, "miris-")).toEqual(["miris-viewerkey-required"]);
        });
    });

    describe("mirisApiBaseUrl", () => {
        test("https://app.miris.com is accepted", () => {
            const mutate = (c: any) => {
                enablePerView(c);
                c.app.addons.useMirisStreaming.mirisApiBaseUrl = "https://app.miris.com";
            };
            expect(resolve(mutate)().app.addons.useMirisStreaming.mirisApiBaseUrl).toBe(
                "https://app.miris.com"
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual([]);
        });

        test("an absent value gets the default", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                delete c.app.addons.useMirisStreaming.mirisApiBaseUrl;
            };
            expect(resolve(mutate)().app.addons.useMirisStreaming.mirisApiBaseUrl).toBe(
                "https://app.miris.com"
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual([]);
        });

        test.each(["", "UNDEFINED"])("%p is rejected as missing", (value) => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.mirisApiBaseUrl = value;
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming requires mirisApiBaseUrl when enabled/
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual(["miris-baseurl-required"]);
        });

        test.each([" https://app.miris.com", "https://app.miris.com ", "   "])(
            "%p with surrounding whitespace is rejected rather than stored",
            (value) => {
                const mutate = (c: any) => {
                    enableStatic(c);
                    c.app.addons.useMirisStreaming.mirisApiBaseUrl = value;
                };
                expect(resolve(mutate)).toThrow(
                    /addons\.useMirisStreaming\.mirisApiBaseUrl has leading or trailing whitespace/
                );
                expect(builderRules(mutate, "miris-baseurl-")).toEqual([
                    "miris-baseurl-whitespace",
                ]);
            }
        );

        test("an unparseable URL is rejected", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.mirisApiBaseUrl = "app.miris.com";
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.mirisApiBaseUrl must be a valid URL/
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual(["miris-baseurl-url"]);
        });

        test("plain http is rejected", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.mirisApiBaseUrl = "http://app.miris.com";
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.mirisApiBaseUrl must use https/
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual(["miris-baseurl-https"]);
        });

        test("the instance metadata endpoint is rejected", () => {
            const mutate = (c: any) => {
                enableStatic(c);
                c.app.addons.useMirisStreaming.mirisApiBaseUrl = "https://169.254.169.254";
            };
            expect(resolve(mutate)).toThrow(
                /addons\.useMirisStreaming\.mirisApiBaseUrl resolves to a loopback, link-local, or private address/
            );
            expect(builderRules(mutate, "miris-baseurl-")).toEqual(["miris-baseurl-private-host"]);
        });
    });
});
