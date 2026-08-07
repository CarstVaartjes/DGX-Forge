export type SourceBundle = {
  archive: Uint8Array;
  sha256: string;
  totalBytes: number;
  files: string[];
};

const encoder = new TextEncoder();

async function digest(value: Uint8Array): Promise<string> {
  const copy = new Uint8Array(value.byteLength);
  copy.set(value);
  const hash = await crypto.subtle.digest("SHA-256", copy.buffer);
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
}

function octal(value: number, width: number): Uint8Array {
  return encoder.encode(value.toString(8).padStart(width - 1, "0") + "\0");
}

function write(target: Uint8Array, offset: number, value: Uint8Array, maximum = value.length): void {
  target.set(value.slice(0, maximum), offset);
}

function tarHeader(path: string, size: number): Uint8Array {
  const header = new Uint8Array(512);
  write(header, 0, encoder.encode(path), 100);
  write(header, 100, octal(0o644, 8));
  write(header, 108, octal(0, 8));
  write(header, 116, octal(0, 8));
  write(header, 124, octal(size, 12));
  write(header, 136, octal(0, 12));
  header.fill(32, 148, 156);
  header[156] = "0".charCodeAt(0);
  write(header, 257, encoder.encode("ustar\0"), 6);
  write(header, 263, encoder.encode("00"), 2);
  const checksum = header.reduce((sum, byte) => sum + byte, 0);
  write(header, 148, encoder.encode(checksum.toString(8).padStart(6, "0") + "\0 "), 8);
  return header;
}

export async function makeSourceBundle(source: Record<string, string>): Promise<SourceBundle> {
  const entries = Object.entries(source)
    .filter(([, value]) => value.length > 0)
    .sort(([left], [right]) => left.localeCompare(right, "en", {usage: "sort"}));
  if (entries.length === 0) throw new Error("The source bundle must contain a Dockerfile");
  const files = await Promise.all(entries.map(async ([path, text]) => {
    if (!/^[A-Za-z0-9._/-]{1,100}$/.test(path) || path.startsWith("/") || path.split("/").includes("..")) {
      throw new Error("The source bundle contains an unsafe path");
    }
    const content = encoder.encode(text);
    return {path, content, sha256: await digest(content)};
  }));
  const totalBytes = files.reduce((sum, file) => sum + file.content.length, 0);
  const manifest = encoder.encode(JSON.stringify({
    files: files.map(file => ({mode: 0o644, path: file.path, sha256: file.sha256, size: file.content.length})),
    schema_version: 1,
    total_bytes: totalBytes,
  }));
  const sha256 = await digest(manifest);
  const archiveBytes = files.reduce((sum, file) => sum + 512 + Math.ceil(file.content.length / 512) * 512, 1024);
  const archive = new Uint8Array(archiveBytes);
  let offset = 0;
  for (const file of files) {
    archive.set(tarHeader(file.path, file.content.length), offset);
    offset += 512;
    archive.set(file.content, offset);
    offset += Math.ceil(file.content.length / 512) * 512;
  }
  return {archive, sha256, totalBytes, files: files.map(file => file.path)};
}
