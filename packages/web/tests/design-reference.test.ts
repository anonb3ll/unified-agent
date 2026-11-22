// @vitest-environment node
import { readFileSync } from "node:fs";
import path from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

const html = readFileSync(path.resolve(__dirname, "../../../docs/designs/command-center-reference.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)![1];
const windows: JSDOM[] = [];

function mount(width = 1280) {
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "https://reference.test", virtualConsole: new VirtualConsole() });
  windows.push(dom);
  const w = dom.window;
  Object.defineProperty(w, "innerWidth", { value: width, writable: true });
  Object.defineProperty(w.HTMLElement.prototype, "inert", {
    get() { return this.hasAttribute("inert"); },
    set(value: boolean) { this.toggleAttribute("inert", value); },
  });
  // jsdom has no layout; actual geometry and responsive CSS are checked in a browser.
  w.HTMLElement.prototype.getClientRects = function () { return (this.closest("[hidden]") ? [] : [{}]) as unknown as DOMRectList; };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event("close")); };
  const fetch = vi.fn();
  Object.assign(w, { fetch });
  const errors: unknown[] = [];
  w.addEventListener("error", event => errors.push(event.error));
  w.eval(script);
  const element = (selector: string) => w.document.querySelector(selector) as HTMLElement;
  const press = (key: string, init: KeyboardEventInit = {}) => w.document.activeElement!.dispatchEvent(new w.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init }));
  return { w, element, press, fetch, errors };
}

afterEach(() => { for (const dom of windows.splice(0)) dom.window.close(); });

describe("Command Center reference interactions", () => {
  it.each(["DEMO-104", "DEMO-107", "DEMO-105"])("opens %s with Enter and Space and restores its trigger", id => {
    const { w, element, press, errors } = mount();
    const row = element(`[data-inspect="${id}"]`);
    for (const key of ["Enter", " "]) {
      row.focus(); press(key);
      expect(element("#insp").hidden).toBe(false);
      expect(w.document.activeElement).toBe(element("#inspector-title"));
      expect(element("#insp header .mono").textContent).toBe(id);
      expect(row.getAttribute("aria-expanded")).toBe("true");
      press("Escape");
      expect(element("#insp").hidden).toBe(true);
      expect(element("#insp").inert).toBe(true);
      expect(w.document.activeElement).toBe(row);
    }
    expect(errors).toEqual([]);
  });

  it("moves attention focus with j/k and traps only an overlay inspector", () => {
    const { w, element, press } = mount();
    const rows = [...w.document.querySelectorAll<HTMLElement>("[data-inspect]")];
    rows[0].focus(); press("j"); expect(w.document.activeElement).toBe(rows[1]);
    press("k"); expect(w.document.activeElement).toBe(rows[0]);
    press("Enter"); press("Tab"); expect(w.document.activeElement).toBe(element("#insp button"));
    press("Tab", { shiftKey: true }); expect(w.document.activeElement).toBe(element('#insp [data-action="Discuss"]'));
    expect(element(".main").inert).toBe(true);
    w.innerWidth = 1440; w.dispatchEvent(new w.Event("resize"));
    expect(element("#insp").getAttribute("role")).toBe("complementary");
    expect(element("#insp").hasAttribute("aria-modal")).toBe(false);
    expect(element(".main").inert).toBe(false);
  });

  it("closes the palette before the inspector and restores focus layer by layer", () => {
    const { w, element, press } = mount();
    element('[data-inspect="DEMO-107"]').focus(); press("Enter");
    press("k", { ctrlKey: true });
    expect(w.document.activeElement).toBe(element("#command-input"));
    expect(element("#insp").inert).toBe(true);
    press("Escape");
    expect(element("#scrim").hidden).toBe(true);
    expect(element("#insp").hidden).toBe(false);
    expect(w.document.activeElement).toBe(element("#inspector-title"));
    press("Escape");
    expect(w.document.activeElement).toBe(element('[data-inspect="DEMO-107"]'));
  });

  it("executes filtered keyboard commands and labels destinations without sample data", () => {
    const { w, element, press } = mount();
    element("#cmdk").focus(); press("k", { ctrlKey: true });
    const input = element("#command-input") as HTMLInputElement;
    input.value = "Delivery"; input.dispatchEvent(new w.Event("input", { bubbles: true }));
    expect(input.getAttribute("aria-activedescendant")).toBe("command-go-delivery");
    press("ArrowDown"); press("Enter");
    expect(element("h1").textContent).toBe("Delivery");
    expect(element(".crumb").textContent).toBe("Delivery");
    expect(element("#overview-content").hidden).toBe(true);
    expect(element("#destination-placeholder").hidden).toBe(false);
    expect(w.document.activeElement).toBe(element("h1"));
    expect(element("#scrim").hidden).toBe(true);
  });

  it("honors destination chords and ignores them while editing the palette", () => {
    const { element, press } = mount();
    element("#cmdk").focus(); press("g"); press("d");
    expect(element("h1").textContent).toBe("Delivery");
    press("k", { ctrlKey: true }); press("g"); press("o");
    expect(element("h1").textContent).toBe("Delivery");
    press("Escape"); press("g"); press("o");
    expect(element("h1").textContent).toBe("Overview");
  });

  it("keeps recorded receipts separate from external-effect evidence", () => {
    const { element, press } = mount();
    element('[data-inspect="DEMO-107"]').focus(); press("Enter");
    const steps = element(".vrail").querySelectorAll("li");
    expect(steps).toHaveLength(8);
    expect(steps[5].querySelector(".node")?.classList.contains("unknown")).toBe(true);
    expect(steps[5].querySelector(".node")?.classList.contains("ok")).toBe(false);
    expect(steps[5].textContent).toContain("merged SHA and loaded target revision not observed");
    const compactRow = [...element("#overview-content").querySelectorAll("tr")].find(row => row.querySelector("td")?.textContent === "Evidence export")!;
    const compactSteps = compactRow.querySelectorAll(".rail i");
    expect(compactSteps).toHaveLength(8);
    expect(compactSteps[5].classList.contains("unknown")).toBe(true);
    expect(compactSteps[5].classList.contains("ok")).toBe(false);
    expect(compactSteps[6].classList.contains("unknown")).toBe(true);
    expect(compactSteps[7].classList.contains("ok")).toBe(true);
    expect(steps[6].querySelector(".node")?.classList.contains("unknown")).toBe(true);
    expect(steps[7].querySelector(".node")?.classList.contains("ok")).toBe(true);
    expect(element(".callout").textContent).toContain("receipt does not establish");
    expect(element("#insp header .pill").textContent).toBe("Blocked");
  });

  it("labels sample actions, sends no request and restores their button", () => {
    const { w, element, press, fetch } = mount();
    const trigger = element('[data-action="Dispatch work"]');
    trigger.focus(); trigger.click();
    expect((element("#sample-action") as HTMLDialogElement).open).toBe(true);
    expect(element("#sample-action").textContent).toContain("No request has been sent");
    press("k", { ctrlKey: true }); expect(element("#scrim").hidden).toBe(true);
    element("#sample-action-close").click();
    expect(w.document.activeElement).toBe(trigger);
    expect(fetch).not.toHaveBeenCalled();
  });
});
