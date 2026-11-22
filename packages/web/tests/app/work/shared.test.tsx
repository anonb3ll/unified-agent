import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WriteFeedback } from "../../../src/app/work/shared";
import { epochSetup } from "../support/epoch";

describe("work: parked write notice (spec §6, 4c0cdae7)", () => {
  // Nothing runs without a person's click; the Work pages use the shared notice.
  it("shows the shared ParkedWriteNotice and never promises an automatic run", () => {
    render(
      <WriteFeedback
        state={{ status: "pending_approval", approvalId: "7", resumable: true, willReplay: false, canRunFromStore: false, replayBlockedReason: null } as never}
        what="The status change"
      />,
      { wrapper: epochSetup().wrapper },
    );
    const notice = screen.getByRole("region", { name: "Approval status" });
    expect(notice).toHaveTextContent(/Waiting for approval/);
    expect(notice).not.toHaveTextContent(/by itself|automatically|will run/i);
    expect(screen.getByRole("link", { name: "Open the approval" })).toHaveAttribute("href", "/inbox?kind=approval&item=7");
  });
});
