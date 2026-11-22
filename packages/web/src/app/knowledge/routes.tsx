/**
 * Knowledge area routes (spec §3, §7, §9.5): memory notes and the vault.
 * Every selection lives in the query string (`?note=<key>`, `?path=<vault
 * path>`), because keys and paths can contain dots and slashes.
 */
import { Navigate, type RouteObject } from "react-router";
import type { PaletteCommand } from "../lib/commands";
import type { RouteHandle } from "../lib/routeHandle";
import { MemoryPage } from "./MemoryPage";
import { VaultPage } from "./VaultPage";

/** Mounted under the area path by `src/app/router.tsx`. */
export const routes: RouteObject[] = [
  { index: true, element: <Navigate to="memory" replace /> },
  { path: "memory", Component: MemoryPage, handle: { crumb: "Memory" } satisfies RouteHandle },
  { path: "vault", Component: VaultPage, handle: { crumb: "Vault" } satisfies RouteHandle },
];

/** Command palette entries for this area. */
export const commands: PaletteCommand[] = [
  { id: "knowledge.new-note", label: "Write a memory note", group: "Knowledge", to: "/knowledge/memory?new=1", keywords: ["new", "save", "memory"] },
  { id: "knowledge.search-memory", label: "Search memory", group: "Knowledge", to: "/knowledge/memory", keywords: ["find", "notes", "recall"] },
  { id: "knowledge.vault", label: "Browse the vault", group: "Knowledge", to: "/knowledge/vault", keywords: ["obsidian", "wiki", "notes"] },
  { id: "knowledge.vault-new", label: "Write a vault note", group: "Knowledge", to: "/knowledge/vault?write=new", keywords: ["new", "obsidian", "inbox"] },
  { id: "knowledge.journal", label: "Add a journal entry", group: "Knowledge", to: "/knowledge/vault?write=journal", keywords: ["log", "daily", "vault"] },
];
