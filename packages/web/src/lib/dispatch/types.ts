/** Dispatch Studio local form state (mirrors legacy #dispatch-form). */
export type DispatchFormState = {
  agent: string;
  project: string;
  parentTaskId: string;
  stackTaskIds: string[];
  prompt: string;
  model: string;
  modelCustom: string;
  context: {
    task_notes: boolean;
    recent_validation: boolean;
    related_pr: boolean;
  };
};

export const MAX_DISPATCH_PROMPT = 1000;

export const DEFAULT_DISPATCH_FORM: DispatchFormState = {
  agent: "",
  project: "",
  parentTaskId: "",
  stackTaskIds: [],
  prompt: "",
  model: "",
  modelCustom: "",
  context: {
    task_notes: true,
    recent_validation: true,
    related_pr: false,
  },
};
