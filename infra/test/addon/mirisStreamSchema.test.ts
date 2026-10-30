/*
 * Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `MIRIS_STREAM_SCHEMA_EXTENSIONS` (config/config.ts) is the `fileKeyTypeRestriction` of the GLOBAL
 * "Miris stream" file metadata schema the add-on stack seeds.
 *
 * `getConfig()` checks the Miris upload pipeline's trigger extensions against the constant, while the
 * add-on stack writes the schema from `dynamodb-metadataschema-miris-stream.json`. An extension added
 * to one and not the other lets the pipeline record stream metadata on a file type the schema does not
 * apply to, or refuses one it does. The synthesized seed is pinned to the same file in
 * `mirisAddonStack.test.ts`, and the backend suite validates the file against the schema models.
 */

import * as fs from "fs";
import * as path from "path";
import * as Config from "../../config/config";
import mirisStreamSchema from "../../lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-stream.json";

const SCHEMA_PATH = path.join(
    __dirname,
    "../../lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-stream.json"
);

describe("Miris stream schema extensions", () => {
    test("MIRIS_STREAM_SCHEMA_EXTENSIONS is the schema's fileKeyTypeRestriction, in order", () => {
        expect(mirisStreamSchema.fileKeyTypeRestriction.split(",")).toEqual([
            ...Config.MIRIS_STREAM_SCHEMA_EXTENSIONS,
        ]);
        // Control: both sides are non-empty, so equality is not two empty lists.
        expect(Config.MIRIS_STREAM_SCHEMA_EXTENSIONS.length).toBeGreaterThan(0);
    });

    test("the schema file is where config.ts says it is", () => {
        expect(fs.existsSync(SCHEMA_PATH)).toBe(true);
        const configSource = fs.readFileSync(
            path.join(__dirname, "../../config/config.ts"),
            "utf8"
        );
        expect(configSource.replace(/\s*\*\s*/g, "")).toContain(
            "infra/lib/nestedStacks/addon/miris/constructs/dynamodb-metadataschema-miris-stream.json"
        );
    });

    test("the schema is a GLOBAL file metadata schema", () => {
        expect(mirisStreamSchema.databaseId).toBe("GLOBAL");
        expect(mirisStreamSchema.metadataSchemaEntityType).toBe("fileMetadata");
        expect(mirisStreamSchema.metadataSchemaId).toBe("miris-stream-file-schema");
    });
});
