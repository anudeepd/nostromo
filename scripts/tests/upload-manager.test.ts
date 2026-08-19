import { afterEach, describe, expect, it, vi } from "vitest";
import { SpeedTracker, UploadManager } from "../../xwing/frontend/src/upload-manager";

interface ProgressLike {
  loaded: number;
  lengthComputable?: boolean;
  total?: number;
}

/**
 * Minimal controllable XMLHttpRequest stand-in. Chunk uploads go through
 * `new XMLHttpRequest()`; tests drive them via `dispatchProgress`/`finish`.
 */
class FakeXHR {
  static all: FakeXHR[] = [];
  static reset(): void {
    FakeXHR.all = [];
  }

  status = 0;
  method = "";
  url = "";
  sentBody: Blob | null = null;
  aborted = false;
  settled = false;

  private listeners = new Map<string, Array<(event?: unknown) => void>>();
  private uploadListeners = new Map<string, Array<(event: ProgressLike) => void>>();

  upload = {
    addEventListener: (type: string, handler: (event: ProgressLike) => void) => {
      const list = this.uploadListeners.get(type) ?? [];
      list.push(handler);
      this.uploadListeners.set(type, list);
    },
    dispatchProgress: (loaded: number) => {
      for (const handler of this.uploadListeners.get("progress") ?? []) {
        handler({ loaded, lengthComputable: true, total: this.sentBody?.size ?? 0 });
      }
    },
  };

  addEventListener(type: string, handler: (event?: unknown) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(handler);
    this.listeners.set(type, list);
  }

  open(method: string, url: string): void {
    this.method = method;
    this.url = url;
  }

  send(body: Blob): void {
    this.sentBody = body;
    FakeXHR.all.push(this);
  }

  abort(): void {
    this.aborted = true;
    this.dispatch("abort");
  }

  finish(status: number): void {
    if (this.settled) return;
    this.settled = true;
    this.status = status;
    this.dispatch("load");
  }

  private dispatch(type: string, event?: unknown): void {
    for (const handler of this.listeners.get(type) ?? []) handler(event);
  }
}

async function settle(): Promise<void> {
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
}

function fetchInitOk(fetcher: ReturnType<typeof vi.fn>): void {
  fetcher.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    if (url === "/_upload/init") {
      const body = JSON.parse(String(init?.body)) as { filename: string };
      return new Response(JSON.stringify({ session_id: `s-${body.filename}` }), { status: 200 });
    }
    if (url.endsWith("/complete")) return new Response(JSON.stringify({ path: "/done" }), { status: 200 });
    return new Response(null, { status: 201 });
  });
}

function managerWith(): UploadManager {
  const fetcher = vi.fn();
  fetchInitOk(fetcher);
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  // Schedule the rAF callback on a microtask so `this.frame` is reset to null
  // after the assignment `this.frame = requestFrame(notify)` completes (real
  // requestAnimationFrame never invokes its callback synchronously).
  return new UploadManager(fetcher as typeof fetch, callback => { queueMicrotask(() => callback(0)); return 1; }, vi.fn());
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  FakeXHR.reset();
});

describe("SpeedTracker", () => {
  it("computes bytes per second across the sampled window", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(1_000);
    const tracker = new SpeedTracker();
    expect(tracker.speed()).toBe(0);
    tracker.sample(0);
    now.mockReturnValue(2_000);
    tracker.sample(10_000);
    expect(tracker.speed()).toBe(10_000);
    now.mockReturnValue(3_000);
    tracker.sample(25_000);
    expect(tracker.speed()).toBe(12_500);
  });

  it("drops samples older than the sliding window", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(0);
    const tracker = new SpeedTracker();
    tracker.sample(0);
    now.mockReturnValue(6_000);
    tracker.sample(6_000);
    now.mockReturnValue(7_000);
    tracker.sample(10_000);
    expect(tracker.speed()).toBe(4_000);
  });

  it("returns 0 after reset", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(1_000);
    const tracker = new SpeedTracker();
    tracker.sample(0);
    now.mockReturnValue(2_000);
    tracker.sample(1_000);
    tracker.reset();
    expect(tracker.speed()).toBe(0);
  });
});

describe("UploadManager global scheduler", () => {
  it("queues uploads when randomUUID is unavailable on HTTP", () => {
    vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
    const manager = new UploadManager(vi.fn(() => new Promise(() => {})) as typeof fetch, vi.fn(), vi.fn());

    expect(() => manager.add([new File(["abc"], "http.txt")], "/", 3)).not.toThrow();
    expect(manager.getSnapshot().items[0]).toMatchObject({ name: "http.txt", status: "queued" });
  });

  it("preserves browser receivers for the native upload primitives", async () => {
    const fetcher = vi.fn(function (this: unknown, input: RequestInfo | URL, _init?: RequestInit): Promise<Response> {
      if (this !== globalThis) throw new TypeError("Illegal invocation");
      const url = String(input);
      if (url === "/_upload/init") return Promise.resolve(new Response(JSON.stringify({ session_id: "native-fetch" }), { status: 200 }));
      if (url.endsWith("/complete")) return Promise.resolve(new Response(JSON.stringify({ path: "/native.txt" }), { status: 200 }));
      return Promise.resolve(new Response(null, { status: 201 }));
    });
    const requestFrame = vi.fn(function (this: unknown, callback: FrameRequestCallback): number {
      if (this !== globalThis) throw new TypeError("Illegal invocation");
      queueMicrotask(() => callback(0));
      return 1;
    });
    const cancelFrame = vi.fn(function (this: unknown): void {
      if (this !== globalThis) throw new TypeError("Illegal invocation");
    });
    vi.stubGlobal("fetch", fetcher);
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    vi.stubGlobal("requestAnimationFrame", requestFrame);
    vi.stubGlobal("cancelAnimationFrame", cancelFrame);
    const manager = new UploadManager();

    manager.add([new File(["works"], "native.txt")], "/", 5);
    manager.setParallel(2);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(1));
    for (const xhr of FakeXHR.all) xhr.finish(204);
    await vi.waitFor(() => expect(manager.getSnapshot().items[0]?.status).toBe("completed"));
    expect(fetcher).toHaveBeenCalledWith("/_upload/init", expect.any(Object));
    expect(requestFrame).toHaveBeenCalled();
    expect(cancelFrame).toHaveBeenCalled();
  });

  it("never exceeds the selected global chunk cap and rotates files", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      if (url === "/_upload/init") {
        const body = JSON.parse(String(init?.body)) as { filename: string };
        return new Response(JSON.stringify({ session_id: `s-${body.filename}` }), { status: 200 });
      }
      if (url.endsWith("/complete")) return new Response(JSON.stringify({ path: "done" }), { status: 200 });
      return new Response(null, { status: 201 });
    });
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    const frames: FrameRequestCallback[] = [];
    const manager = new UploadManager(fetcher as typeof fetch, callback => { frames.push(callback); return frames.length; }, vi.fn());
    manager.setParallel(2);
    manager.add([new File(["abcdef"], "a.txt"), new File(["ghijkl"], "b.txt")], "/", 2);

    const active = () => FakeXHR.all.filter(xhr => !xhr.settled).length;
    await vi.waitFor(() => expect(active()).toBe(2));
    expect(FakeXHR.all).toHaveLength(2);
    expect(new Set(FakeXHR.all.map(xhr => xhr.url.split("/")[2]))).toEqual(new Set(["s-a.txt", "s-b.txt"]));

    let maximum = active();
    while (FakeXHR.all.some(xhr => !xhr.settled)) {
      for (const xhr of FakeXHR.all) {
        if (!xhr.settled) {
          xhr.finish(204);
          await settle();
        }
      }
      maximum = Math.max(maximum, active());
    }
    expect(maximum).toBe(2);
  });

  it("publishes terminal cancellation immediately", () => {
    const frames: FrameRequestCallback[] = [];
    const manager = new UploadManager(vi.fn(() => new Promise(() => {})) as typeof fetch, callback => { frames.push(callback); return frames.length; }, vi.fn());
    manager.add([new File(["abc"], "a.txt")], "/", 2);
    const item = manager.getSnapshot().items[0];
    expect(item).toBeDefined();
    manager.cancel(item!.id);
    expect(manager.getSnapshot().items[0]?.status).toBe("cancelled");
  });

  it("dismisses successful uploads without hiding failures", async () => {
    const manager = managerWith();
    manager.setParallel(2);
    manager.add([new File(["ok"], "successful.txt"), new File(["no"], "failed.txt")], "/", 2);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(2));
    for (const xhr of FakeXHR.all) {
      xhr.finish(xhr.url.includes("failed") ? 400 : 204);
    }
    await vi.waitFor(() => expect(new Set(manager.getSnapshot().items.map(item => item.status))).toEqual(new Set(["completed", "failed"])));

    manager.dismissSuccessful();

    expect(manager.getSnapshot().items.map(item => item.status)).toEqual(["failed"]);
  });

  it("replays chunks after a failure so retry cannot skip data", async () => {
    const manager = managerWith();
    manager.setParallel(1);
    manager.add([new File(["abcd"], "a.txt")], "/", 2);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(1));
    FakeXHR.all[0]!.finish(400);
    await vi.waitFor(() => expect(manager.getSnapshot().items[0]?.status).toBe("failed"));

    const id = manager.getSnapshot().items[0]!.id;
    manager.retry(id);
    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(2));
    FakeXHR.all[1]!.finish(204);
    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(3));
    FakeXHR.all[2]!.finish(204);
    await vi.waitFor(() => expect(manager.getSnapshot().items[0]?.status).toBe("completed"));
    expect(FakeXHR.all.map(xhr => xhr.url.split("/").at(-1))).toEqual(["0", "0", "1"]);
  });
});

describe("XHR upload progress", () => {
  it("advances item.uploaded intra-chunk from progress events and exposes speed", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const manager = managerWith();
    manager.setParallel(1);
    manager.add([new File(["x".repeat(10_000)], "a.txt")], "/", 10_000);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(1));
    const xhr = FakeXHR.all[0]!;
    xhr.upload.dispatchProgress(3_000);
    await settle();
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(3_000);
    now.mockReturnValue(2_000);
    xhr.upload.dispatchProgress(6_000);
    await settle();
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(6_000);
    expect(manager.getSnapshot().items[0]?.speed).toBe(3_000);
    xhr.finish(204);
    await vi.waitFor(() => expect(manager.getSnapshot().items[0]?.status).toBe("completed"));
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(10_000);
  });

  it("clamps displayed progress so it never regresses during an in-flight retry", async () => {
    const manager = managerWith();
    manager.setParallel(1);
    manager.add([new File(["abcdefgh"], "a.txt")], "/", 4);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(1));
    FakeXHR.all[0]!.upload.dispatchProgress(3);
    await settle();
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(3);
    // Retryable failure: the re-sent chunk restarts from 0 loaded, so the
    // displayed count is held at the last reported value until it catches up.
    FakeXHR.all[0]!.finish(500);
    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(2), { timeout: 3000 });
    FakeXHR.all[1]!.upload.dispatchProgress(1);
    await settle();
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(3);
    FakeXHR.all[1]!.upload.dispatchProgress(4);
    await settle();
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(4);
    FakeXHR.all[1]!.finish(204);
    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(3));
    FakeXHR.all[2]!.finish(204);
    await vi.waitFor(() => expect(manager.getSnapshot().items[0]?.status).toBe("completed"));
    expect(manager.getSnapshot().items[0]?.uploaded).toBe(8);
  });

  it("aborts the in-flight XHR when the upload is cancelled", async () => {
    const manager = managerWith();
    manager.setParallel(1);
    manager.add([new File(["abcdefgh"], "a.txt")], "/", 4);

    await vi.waitFor(() => expect(FakeXHR.all).toHaveLength(1));
    const xhr = FakeXHR.all[0]!;
    expect(xhr.aborted).toBe(false);
    manager.cancel(manager.getSnapshot().items[0]!.id);
    expect(xhr.aborted).toBe(true);
    expect(manager.getSnapshot().items[0]?.status).toBe("cancelled");
  });
});
