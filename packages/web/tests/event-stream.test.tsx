import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribeEventStream } from "../src/lib/eventStream";
import { STREAM_MIN_GAP_MS, STREAM_RECONNECT_MS, useStreamInvalidation } from "../src/lib/polling";

class Source {
  static instances: Source[] = [];
  readyState = 0;
  closed = false;
  target = new EventTarget();
  constructor(public url: string) { Source.instances.push(this); }
  addEventListener(type: string, listener: EventListener) { this.target.addEventListener(type, listener); }
  close() { this.closed = true; this.readyState = 2; }
  emit(type: string, channel = "agent.task.updated", lastEventId = "") {
    this.target.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ channel }), lastEventId }));
  }
}

const stops: (() => void)[] = [];
function subscribe(channels: string, onMessage = vi.fn(), onCursor = vi.fn()) {
  stops.push(subscribeEventStream(`/api/events/stream?channels=${channels}`, (stream) => {
    stream.addEventListener("message", onMessage);
    stream.addEventListener("cursor", onCursor);
  }));
  return { onMessage, onCursor };
}
function hidden(value: boolean) {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: value ? "hidden" : "visible" });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => { Source.instances = []; vi.stubGlobal("EventSource", Source); });
afterEach(() => { stops.splice(0).forEach((stop) => stop()); hidden(false); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("shared Control Center event stream", () => {
  it("uses one socket for many panes and delivers only their selected channels", () => {
    const tasks = subscribe("agent.task.*");
    const approvals = subscribe("agent.approval.*");
    const node = subscribe("agent.node");
    const activity = subscribe("");
    expect(Source.instances).toHaveLength(1);
    expect(Source.instances[0].url).toBe("/api/events/stream");
    const source = Source.instances[0];
    source.emit("message", "agent.task.updated");
    source.emit("message", "agent.approval.requested");
    source.emit("message", "agent.node.heartbeat");
    source.emit("message", "agent.node");
    expect(tasks.onMessage).toHaveBeenCalledTimes(1);
    expect(approvals.onMessage).toHaveBeenCalledTimes(1);
    expect(node.onMessage).toHaveBeenCalledTimes(1);
    expect(activity.onMessage).toHaveBeenCalledTimes(4);
  });

  it("keeps other panes connected on cleanup and closes on the last unsubscribe", () => {
    const tasks = subscribe("agent.task.*");
    const approvals = subscribe("agent.approval.*");
    const source = Source.instances[0];
    stops[0](); stops[0](); // Cleanup is idempotent.
    expect(source.closed).toBe(false);
    source.emit("message", "agent.approval.resolved");
    source.emit("message", "agent.task.updated");
    expect(approvals.onMessage).toHaveBeenCalledTimes(1);
    expect(tasks.onMessage).not.toHaveBeenCalled();
    stops[1]();
    expect(source.closed).toBe(true);
    subscribe("agent.task.*");
    expect(Source.instances).toHaveLength(2);
    stops[0](); // Old cleanup cannot close a later connection.
    expect(Source.instances[1].closed).toBe(false);
  });

  it("shares refusal recovery and resumes from even an unrelated event's cursor", () => {
    vi.useFakeTimers();
    const tasks = subscribe("agent.task.*");
    const approvals = subscribe("agent.approval.*");
    const first = Source.instances[0];
    first.emit("message", "agent.node", "seq:42");
    first.readyState = 2; first.emit("error");
    vi.advanceTimersByTime(STREAM_RECONNECT_MS);
    expect(Source.instances).toHaveLength(2);
    expect(Source.instances[1].url).toBe("/api/events/stream?after_seq=42");
    Source.instances[1].emit("message", "agent.task.updated");
    Source.instances[1].emit("message", "agent.approval.requested");
    expect(tasks.onMessage).toHaveBeenCalledTimes(1);
    expect(approvals.onMessage).toHaveBeenCalledTimes(1);
    Source.instances[1].readyState = 2; Source.instances[1].emit("error");
    stops.splice(0).forEach((stop) => stop());
    vi.advanceTimersByTime(10 * STREAM_RECONNECT_MS);
    expect(Source.instances).toHaveLength(2);
  });

  it("gives a late pane the current connection state without replaying old messages", () => {
    subscribe("agent.task.*");
    Source.instances[0].emit("cursor", "", "seq:7");
    Source.instances[0].emit("message", "agent.task.updated");
    const late = subscribe("agent.task.*");
    expect(late.onCursor).toHaveBeenCalledTimes(1);
    expect(late.onMessage).not.toHaveBeenCalled();
    Source.instances[0].emit("error");
    const disconnected = subscribe("agent.task.*");
    expect(disconnected.onCursor).not.toHaveBeenCalled();
  });

  it("coalesces each pane, ignores unrelated channels, and holds all hidden-tab refetches", () => {
    vi.useFakeTimers();
    const taskRefresh = vi.fn(); const approvalRefresh = vi.fn();
    const tasks = renderHook(() => useStreamInvalidation(taskRefresh, { channels: ["agent.task.*"] }));
    const approvals = renderHook(() => useStreamInvalidation(approvalRefresh, { channels: ["agent.approval.*"] }));
    stops.push(tasks.unmount, approvals.unmount);
    expect(Source.instances).toHaveLength(1);
    const source = Source.instances[0];
    source.emit("message", "agent.gateway.allow");
    expect(taskRefresh).not.toHaveBeenCalled(); expect(approvalRefresh).not.toHaveBeenCalled();
    for (let i = 0; i < 100; i++) source.emit("message", "agent.task.updated");
    expect(taskRefresh).toHaveBeenCalledTimes(1);
    expect(approvalRefresh).not.toHaveBeenCalled();
    act(() => hidden(true));
    act(() => vi.advanceTimersByTime(STREAM_MIN_GAP_MS));
    for (let i = 0; i < 100; i++) source.emit("message", "agent.approval.requested");
    expect(taskRefresh).toHaveBeenCalledTimes(1); expect(approvalRefresh).not.toHaveBeenCalled();
    act(() => hidden(false));
    expect(taskRefresh).toHaveBeenCalledTimes(2); expect(approvalRefresh).toHaveBeenCalledTimes(1);
  });
});
