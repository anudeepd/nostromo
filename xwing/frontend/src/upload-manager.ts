import type { Parallelism } from "./types";

const RETRY_DELAYS = [750, 1500, 3000] as const;
const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

export type UploadStatus =
  | "queued"
  | "preparing"
  | "uploading"
  | "retrying"
  | "completed"
  | "failed"
  | "cancelled";

export interface UploadItem {
  id: string;
  name: string;
  relativePath: string;
  destination: string;
  size: number;
  uploaded: number;
  speed: number;
  status: UploadStatus;
  error?: string | undefined;
}

interface InternalItem extends UploadItem {
  file: File;
  sessionId?: string;
  chunkSize: number;
  chunkCount: number;
  nextChunk: number;
  completedChunks: Set<number>;
  controller: AbortController;
  /** Bytes currently in flight per chunk index (from the XHR `progress` event). */
  inflightBytes: Map<number, number>;
  /** Monotonic high-water mark of displayed uploaded bytes (clamps retry regressions). */
  lastReported: number;
  tracker: SpeedTracker;
}

export class SpeedTracker {
  private samples: Array<{ t: number; b: number }> = [];
  private readonly windowMs = 5000;
  private readonly maxSamples = 20;

  sample(bytes: number): void {
    const now = Date.now();
    const cutoff = now - this.windowMs;
    this.samples = [...this.samples, { t: now, b: bytes }].filter(sample => sample.t >= cutoff);
    if (this.samples.length > this.maxSamples) {
      this.samples = this.samples.slice(this.samples.length - this.maxSamples);
    }
  }

  speed(): number {
    if (this.samples.length < 2) return 0;
    const first = this.samples[0]!;
    const last = this.samples[this.samples.length - 1]!;
    const elapsedSeconds = (last.t - first.t) / 1000;
    if (elapsedSeconds <= 0) return 0;
    return (last.b - first.b) / elapsedSeconds;
  }

  reset(): void {
    this.samples = [];
  }
}

export interface UploadSnapshot {
  items: readonly UploadItem[];
  active: number;
  parallel: Parallelism;
}

type Fetch = typeof fetch;

function uploadId(): string {
  const timestamp = Date.now().toString(36);
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === "function") {
    return `${timestamp}-${randomUUID.call(globalThis.crypto)}`;
  }
  return `${timestamp}-${Math.random().toString(36).slice(2)}`;
}

export class UploadManager {
  private readonly items = new Map<string, InternalItem>();
  private readonly listeners = new Set<() => void>();
  private active = 0;
  private parallel: Parallelism = 4;
  private cursor = 0;
  private frame: number | null = null;
  private snapshot: UploadSnapshot = { items: [], active: 0, parallel: 4 };

  constructor(
    private readonly fetcher: Fetch = globalThis.fetch.bind(globalThis),
    private readonly requestFrame: (callback: FrameRequestCallback) => number = globalThis.requestAnimationFrame.bind(globalThis),
    private readonly cancelFrame: (handle: number) => void = globalThis.cancelAnimationFrame.bind(globalThis),
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): UploadSnapshot => this.snapshot;

  setParallel(value: Parallelism): void {
    this.parallel = value;
    this.flush(true);
    this.pump();
  }

  add(files: Iterable<File>, destination: string, chunkSize: number): void {
    for (const file of files) {
      const relativePath = file.webkitRelativePath || file.name;
      const id = uploadId();
      this.items.set(id, {
        id,
        name: file.name,
        relativePath,
        size: file.size,
        uploaded: 0,
        speed: 0,
        status: "queued",
        file,
        destination,
        chunkSize: Math.max(1, chunkSize),
        chunkCount: Math.max(1, Math.ceil(file.size / Math.max(1, chunkSize))),
        nextChunk: 0,
        completedChunks: new Set(),
        controller: new AbortController(),
        inflightBytes: new Map(),
        lastReported: 0,
        tracker: new SpeedTracker(),
      });
    }
    this.flush(true);
    void this.prepareQueued();
  }

  cancel(id: string): void {
    const item = this.items.get(id);
    if (!item || item.status === "completed") return;
    item.controller.abort();
    item.status = "cancelled";
    this.flush(true);
    this.pump();
  }

  retry(id: string): void {
    const item = this.items.get(id);
    if (!item || (item.status !== "failed" && item.status !== "cancelled")) return;
    item.controller = new AbortController();
    item.error = undefined;
    // A failed request may have advanced the scheduler beyond its chunk. Start
    // over so every chunk is present before completion; duplicate PUTs are
    // idempotent server-side and this also recovers a failed completion call.
    item.nextChunk = 0;
    item.completedChunks.clear();
    item.uploaded = 0;
    item.inflightBytes.clear();
    item.lastReported = 0;
    item.tracker.reset();
    item.speed = 0;
    item.status = item.sessionId ? "uploading" : "queued";
    this.flush(true);
    void this.prepareQueued();
    this.pump();
  }

  dismissCompleted(): void {
    for (const [id, item] of this.items) {
      if (item.status === "completed" || item.status === "cancelled") this.items.delete(id);
    }
    this.flush(true);
  }

  dismissSuccessful(): void {
    for (const [id, item] of this.items) {
      if (item.status === "completed") this.items.delete(id);
    }
    this.flush(true);
  }

  hasActive(): boolean {
    return [...this.items.values()].some(item =>
      ["queued", "preparing", "uploading", "retrying"].includes(item.status),
    );
  }

  private async prepareQueued(): Promise<void> {
    const queued = [...this.items.values()].filter(item => item.status === "queued");
    await Promise.all(queued.map(item => this.initialize(item)));
    this.pump();
  }

  private async initialize(item: InternalItem): Promise<void> {
    item.status = "preparing";
    this.flush();
    try {
      const directoryParts = item.relativePath.split("/").slice(0, -1);
      const destination = directoryParts.length
        ? `${item.destination.replace(/\/$/, "")}/${directoryParts.join("/")}`
        : item.destination;
      await this.ensureDirectories(destination, item.destination, item.controller.signal);
      const response = await this.fetcher("/_upload/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: item.name,
          total_chunks: item.chunkCount,
          chunk_size: item.chunkSize,
          dir: destination,
        }),
        signal: item.controller.signal,
      });
      if (!response.ok) throw new Error(await responseMessage(response));
      const data = (await response.json()) as { session_id?: string; ignored?: boolean };
      if (data.ignored) {
        item.status = "completed";
        item.uploaded = item.size;
      } else if (data.session_id) {
        item.sessionId = data.session_id;
        item.status = "uploading";
      } else {
        throw new Error("Upload session was not created");
      }
    } catch (error) {
      if (item.controller.signal.aborted) item.status = "cancelled";
      else {
        item.status = "failed";
        item.error = errorMessage(error);
      }
    }
    this.flush(true);
  }

  private async ensureDirectories(path: string, base: string, signal: AbortSignal): Promise<void> {
    const baseSegments = base.split("/").filter(Boolean);
    const segments = path.split("/").filter(Boolean);
    for (let index = baseSegments.length; index < segments.length; index += 1) {
      const target = `/${segments.slice(0, index + 1).map(encodeURIComponent).join("/")}/`;
      const response = await this.fetcher(target, { method: "MKCOL", signal });
      if (!response.ok && response.status !== 405) throw new Error(await responseMessage(response));
    }
  }

  private pump(): void {
    while (this.active < this.parallel) {
      const ready = [...this.items.values()].filter(
        item => item.status === "uploading" && item.nextChunk < item.chunkCount,
      );
      if (!ready.length) break;
      const item = ready[this.cursor % ready.length];
      if (!item) break;
      this.cursor += 1;
      const chunk = item.nextChunk++;
      this.active += 1;
      void this.sendChunk(item, chunk).finally(() => {
        this.active -= 1;
        this.flush(true);
        this.pump();
      });
    }
  }

  private async sendChunk(item: InternalItem, index: number): Promise<void> {
    if (!item.sessionId || item.controller.signal.aborted) return;
    const start = index * item.chunkSize;
    const end = Math.min(item.size, start + item.chunkSize);
    const body = item.file.slice(start, end);
    try {
      await retry(async () => {
        await this.uploadBlob(item, index, `/_upload/${item.sessionId}/${index}`, body);
      }, item);
      item.completedChunks.add(index);
      item.inflightBytes.delete(index);
      item.uploaded = Math.min(item.size, item.uploaded + body.size);
      this.sampleProgress(item);
      this.flush();
      if (item.completedChunks.size === item.chunkCount) await this.complete(item);
    } catch (error) {
      item.inflightBytes.delete(index);
      if (item.controller.signal.aborted) item.status = "cancelled";
      else {
        item.status = "failed";
        item.error = errorMessage(error);
      }
      this.flush(true);
    }
  }

  /**
   * PUT a chunk over XMLHttpRequest so upload progress is observable.
   * `fetch()` exposes no upload progress events, which left the UI stuck on
   * "uploading" until the whole chunk finished.
   */
  private uploadBlob(item: InternalItem, index: number, url: string, body: Blob): Promise<void> {
    return new Promise((resolve, reject) => {
      if (item.controller.signal.aborted) {
        reject(abortError());
        return;
      }
      item.inflightBytes.set(index, 0);
      const xhr = new XMLHttpRequest();
      const onAbort = () => xhr.abort();
      item.controller.signal.addEventListener("abort", onAbort, { once: true });
      const stopListening = () => item.controller.signal.removeEventListener("abort", onAbort);

      xhr.upload.addEventListener("progress", (event: ProgressEvent) => {
        const loaded = Math.min(body.size, event.loaded);
        item.inflightBytes.set(index, loaded);
        this.sampleProgress(item);
        this.flush();
      });

      xhr.addEventListener("load", () => {
        stopListening();
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          const error = new Error(`Upload failed (${xhr.status})`) as Error & { status?: number };
          error.status = xhr.status;
          reject(error);
        }
      });
      xhr.addEventListener("error", () => {
        stopListening();
        reject(new Error("network error"));
      });
      xhr.addEventListener("abort", () => {
        stopListening();
        reject(abortError());
      });

      xhr.open("PUT", url, true);
      xhr.send(body);
    });
  }

  private inflightTotal(item: InternalItem): number {
    let total = 0;
    for (const bytes of item.inflightBytes.values()) total += bytes;
    return total;
  }

  /** Displayed uploaded bytes: completed base + in-flight bytes, clamped monotonic. */
  private displayedUploaded(item: InternalItem): number {
    return Math.max(item.lastReported, Math.min(item.size, item.uploaded + this.inflightTotal(item)));
  }

  private sampleProgress(item: InternalItem): void {
    const total = item.uploaded + this.inflightTotal(item);
    item.lastReported = Math.max(item.lastReported, Math.min(item.size, total));
    item.tracker.sample(total);
    item.speed = item.tracker.speed();
  }

  private async complete(item: InternalItem): Promise<void> {
    if (!item.sessionId) return;
    try {
      const response = await this.fetcher(`/_upload/${item.sessionId}/complete`, {
        method: "POST",
        signal: item.controller.signal,
      });
      if (!response.ok) throw new Error(await responseMessage(response));
      item.status = "completed";
      item.uploaded = item.size;
    } catch (error) {
      item.status = item.controller.signal.aborted ? "cancelled" : "failed";
      item.error = errorMessage(error);
    }
    this.flush(true);
  }

  private flush(immediate = false): void {
    const notify = () => {
      this.frame = null;
      this.snapshot = {
        items: [...this.items.values()].map(item => {
          const {
            file: _file,
            controller: _controller,
            completedChunks: _chunks,
            inflightBytes: _inflight,
            lastReported: _lastReported,
            tracker: _tracker,
            ...rest
          } = item;
          return { ...rest, uploaded: this.displayedUploaded(item) };
        }),
        active: this.active,
        parallel: this.parallel,
      };
      for (const listener of this.listeners) listener();
    };
    if (immediate) {
      if (this.frame !== null) this.cancelFrame(this.frame);
      notify();
    } else if (this.frame === null) {
      this.frame = this.requestFrame(notify);
    }
  }
}

async function retry(action: () => Promise<void>, item: InternalItem): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await action();
      item.status = "uploading";
      return;
    } catch (error) {
      const status = (error as { status?: number }).status;
      if (item.controller.signal.aborted || attempt >= RETRY_DELAYS.length || (status && !RETRYABLE.has(status))) throw error;
      item.status = "retrying";
      await new Promise(resolve => setTimeout(resolve, RETRY_DELAYS[attempt]));
    }
  }
}

async function responseMessage(response: Response): Promise<string> {
  try {
    const value = (await response.json()) as { detail?: string };
    return value.detail || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Upload failed";
}

function abortError(): Error {
  return new DOMException("Aborted", "AbortError");
}
