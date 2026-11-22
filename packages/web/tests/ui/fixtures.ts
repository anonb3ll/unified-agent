export interface MockTask {
  id: number;
  title: string;
  status: "open" | "doing" | "blocked" | "done";
  project: string;
  age_s: number;
}

export const MOCK_TASKS: MockTask[] = [
  {
    id: 101,
    title: "Modernize Control Center shell",
    status: "doing",
    project: "unified-agent",
    age_s: 300,
  },
  {
    id: 102,
    title: "Browser bridge alignment",
    status: "open",
    project: "browser",
    age_s: 1800,
  },
  {
    id: 103,
    title: "Zulip notification webhook failure",
    status: "blocked",
    project: "unified-agent",
    age_s: 86400,
  },
  {
    id: 104,
    title: "Release verification gate",
    status: "done",
    project: "sample-store",
    age_s: 172800,
  },
];

export interface MockApproval {
  id: number;
  tool: string;
  requested_by: string;
  bucket: "pending" | "approved" | "denied";
}

export const MOCK_APPROVALS: MockApproval[] = [
  { id: 91, tool: "run_cmd", requested_by: "Desktop_Agent1", bucket: "pending" },
  { id: 92, tool: "fs_write", requested_by: "Desktop_Agent2", bucket: "approved" },
];
