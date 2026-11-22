// @vitest-environment node
import { readFileSync } from "node:fs";
import path from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

const html = readFileSync(path.resolve(__dirname, "../../../docs/designs/unified-agent-overhaul-prototype.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)![1];
const windows: JSDOM[] = [];
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

async function mount(savedView?: string) {
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "https://prototype.test", virtualConsole: new VirtualConsole() });
  windows.push(dom);
  const w = dom.window;
  const instances: { destroy: ReturnType<typeof vi.fn>; options: { onError: () => void } }[] = [];
  Object.defineProperty(w, "isSecureContext", { value: true });
  Object.defineProperty(w.navigator, "gpu", { value: {} });
  Object.assign(w, {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    IntersectionObserver: class { observe() {} disconnect() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    // The optional provider is mocked; the actual prototype script and DOM execute.
    __shaderModule: Promise.resolve({ createShader: async (_canvas: unknown, _scene: unknown, options: { onError: () => void }) => {
      const instance = { destroy: vi.fn(), pause() {}, resume() {}, resize() {}, getFailureReason: () => null, options };
      instances.push(instance);
      return instance;
    } }),
    openai: { widgetState: { privateContent: { view: savedView } } },
    getComputedStyle: () => ({ color: "rgb(17, 17, 27)" }),
  });
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const errors: unknown[] = [];
  w.addEventListener("error", e => errors.push(e.error));
  const providerImport = "import('https://cdn.jsdelivr.net/npm/shaders@4.0.0/dist/js/bundle.js')";
  expect(script).toContain(providerImport);
  w.eval(script.replace(providerImport, "window.__shaderModule"));
  await settle();
  return { w, instances, errors };
}

afterEach(() => { for (const dom of windows.splice(0)) dom.window.close(); });

describe("design prototype lifecycle and keyboard behavior", () => {
  it.each(["__proto__", "constructor", "toString"])("rejects inherited saved view %s at load and on widget update", async view => {
    const { w, errors } = await mount(view);
    expect(w.document.querySelector("#ua-breadcrumb")?.textContent).toBe("Overview");
    w.dispatchEvent(new w.CustomEvent("openai:set_globals", { detail: { globals: { widgetState: { privateContent: { view } } } } }));
    expect(w.document.querySelector("#ua-breadcrumb")?.textContent).toBe("Overview");
    expect(errors).toEqual([]);
  });

  it("keeps keyboard focus on the replacement delivery tab", async () => {
    const { w } = await mount();
    (w.document.querySelector('[data-action="work"]') as HTMLButtonElement).click();
    const tab = w.document.querySelector('[data-tab="activity"]') as HTMLButtonElement;
    tab.focus(); tab.click();
    expect(w.document.activeElement).not.toBe(tab);
    expect((w.document.activeElement as HTMLElement).dataset.tab).toBe("activity");
    expect(w.document.querySelector('[data-tab="activity"]')?.getAttribute("aria-pressed")).toBe("true");
  });

  it("disposes the old accent and recreates it after a persisted browser return", async () => {
    const { w, instances } = await mount();
    expect(instances).toHaveLength(1);
    w.dispatchEvent(new w.PageTransitionEvent("pagehide", { persisted: true }));
    expect(instances[0].destroy).toHaveBeenCalledOnce();
    w.dispatchEvent(new w.PageTransitionEvent("pageshow", { persisted: true }));
    await settle();
    expect(instances).toHaveLength(2);
  });

  it("disposes recoverable provider errors instead of hiding a running accent", async () => {
    const { w, instances } = await mount();
    instances[0].options.onError();
    await settle();
    expect(instances[0].destroy).toHaveBeenCalledOnce();
    expect(w.document.querySelector(".accent-art")?.getAttribute("data-shader-state")).toBe("static");
    expect((w.document.querySelector('[data-action="motion"]') as HTMLButtonElement).disabled).toBe(true);
  });
});
