#!/usr/bin/env node
/** Remote work-product adapter for the first-party unified-agent CLI. */

import { ControlCenterClient } from "../dist/index.js";

const [action, baseUrl, tokenEnv] = process.argv.slice(2);
if (!new Set(["submit", "release"]).has(action) || !baseUrl || !tokenEnv) {
  process.stderr.write("usage: work-product-sdk {submit|release} HUB_URL TOKEN_ENV\n");
  process.exitCode = 2;
} else {
  const bearerToken = process.env[tokenEnv];
  if (!bearerToken) {
    process.stderr.write(`work-product SDK: ${tokenEnv} is unset or empty\n`);
    process.exitCode = 2;
  } else {
    try {
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const client = new ControlCenterClient({ baseUrl, bearerToken });
      const result = action === "submit"
        ? await client.submitWorkProduct(body)
        : await client.releaseWorkProduct(body.work_product_id);
      process.stdout.write(`${JSON.stringify(result)}\n`);
    } catch (error) {
      process.stderr.write(`work-product SDK: ${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 2;
    }
  }
}
