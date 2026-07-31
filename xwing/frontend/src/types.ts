export const DIRECTORY_MEDIA_TYPE = "application/vnd.xwing.directory+json";

export type Parallelism = 1 | 2 | 4 | 8;

export interface Breadcrumb {
  name: string;
  path: string;
}

export interface XwingFile {
  name: string;
  path: string;
  kind: "file" | "directory";
  size: number | null;
  modified: string | null;
  editable: boolean;
}

export interface XwingBootstrapV1 {
  version: 1;
  path: string;
  breadcrumbs: Breadcrumb[];
  user: { name: string; authenticated: boolean };
  admin?: boolean;
  permissions: { read: boolean; write: boolean; delete: boolean };
  files: XwingFile[];
  upload: { chunkSize: number; parallelDefault: Parallelism };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseBootstrap(value: unknown): XwingBootstrapV1 {
  if (!isRecord(value) || value.version !== 1 || typeof value.path !== "string") {
    throw new Error("Unsupported X-wing directory response");
  }
  if (!Array.isArray(value.breadcrumbs) || !Array.isArray(value.files)) {
    throw new Error("Invalid X-wing directory response");
  }
  if (!isRecord(value.user) || !isRecord(value.permissions) || !isRecord(value.upload)) {
    throw new Error("Invalid X-wing directory response");
  }
  for (const file of value.files) {
    if (
      !isRecord(file) ||
      typeof file.name !== "string" ||
      typeof file.path !== "string" ||
      (file.kind !== "file" && file.kind !== "directory") ||
      typeof file.editable !== "boolean"
    ) {
      throw new Error("Invalid file entry in directory response");
    }
  }
  return value as unknown as XwingBootstrapV1;
}

export function encodePath(path: string): string {
  const trailing = path.length > 1 && path.endsWith("/");
  const encoded = path
    .split("/")
    .filter(Boolean)
    .map(segment => encodeURIComponent(decodeURIComponent(segment)))
    .join("/");
  return `/${encoded}${trailing && encoded ? "/" : ""}`;
}
