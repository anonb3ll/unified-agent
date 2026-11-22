/**
 * Export every PROTOCOL_SCHEMAS entry to generated/json-schema/<Name>.json
 * using Zod 4's built-in z.toJSONSchema.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

import { PROTOCOL_SCHEMAS } from "../src/schemas.ts";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "generated", "json-schema");

mkdirSync(outDir, { recursive: true });

const index: Record<string, string> = {};

for (const [name, schema] of Object.entries(PROTOCOL_SCHEMAS)) {
  const jsonSchema = z.toJSONSchema(schema, {
    target: "draft-07",
    io: "input",
  });
  const fileName = `${name}.json`;
  const path = join(outDir, fileName);
  writeFileSync(path, `${JSON.stringify(jsonSchema, null, 2)}\n`, "utf8");
  index[name] = `./${fileName}`;
  console.log(`wrote ${fileName}`);
}

writeFileSync(
  join(outDir, "index.json"),
  `${JSON.stringify({ $schema: "https://json-schema.org/draft-07/schema", schemas: index }, null, 2)}\n`,
  "utf8",
);
console.log("wrote index.json");
