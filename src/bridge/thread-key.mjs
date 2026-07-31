const UUID_PATTERN =
  "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const THREAD_KEY_PATTERN = new RegExp(
  `^(?:${UUID_PATTERN}|client-new-thread:${UUID_PATTERN})$`,
  "i"
);

export function validateThreadId(value) {
  const normalized = String(value ?? "").replace(/^local:/, "");
  if (!THREAD_KEY_PATTERN.test(normalized)) {
    throw new Error("Invalid Codex thread id");
  }
  return normalized;
}

export function decodeThreadPathSegment(segment) {
  let decoded;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    throw new Error("Invalid encoded Codex thread id");
  }
  return validateThreadId(decoded);
}

export function localThreadKey(value) {
  return `local:${validateThreadId(value)}`;
}
