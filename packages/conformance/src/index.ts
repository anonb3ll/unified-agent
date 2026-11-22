import { PolicyDecision } from "@unified-agent/protocol";
import { z } from "zod";

export const PathConformanceCase = z.object({
  id: z.string().min(1),
  path: z.string().min(1),
  expected: PolicyDecision,
});
export type PathConformanceCase = z.infer<typeof PathConformanceCase>;

export const PathConformanceFixture = z.object({
  schema_version: z.literal("ua.conformance.v1"),
  suite: z.literal("workspace-path-containment"),
  cases: z.array(PathConformanceCase).min(1),
});
export type PathConformanceFixture = z.infer<typeof PathConformanceFixture>;

export const ShellCheckDecision = z.object({
  ok: z.boolean(),
  verdict: z.enum(["ok", "warn", "block"]),
  mutates: z.enum(["no", "maybe", "yes"]),
  finding_codes: z.array(z.string()),
});
export type ShellCheckDecision = z.infer<typeof ShellCheckDecision>;

export const ShellConformanceCase = z.object({
  id: z.string().min(1),
  command: z.string().min(1),
  cwd: z.string().min(1),
  expected: ShellCheckDecision,
});
export type ShellConformanceCase = z.infer<typeof ShellConformanceCase>;

export const ShellConformanceFixture = z.object({
  schema_version: z.literal("ua.conformance.v1"),
  suite: z.literal("shell-run-cmd-check"),
  cases: z.array(ShellConformanceCase).min(1),
});
export type ShellConformanceFixture = z.infer<typeof ShellConformanceFixture>;
