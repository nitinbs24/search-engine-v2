// Helpers for the download route, kept apart from it so they can be tested with `node --test`.

// A "." or ".." segment is resolved by the URL parser, which would turn
// uploads/../other/x into a request for another bucket.
export function isSafeKey(key) {
  return key.split("/").every((segment) => segment !== "." && segment !== "..");
}

// AWS signature V4 needs !'()* percent-encoded; encodeURIComponent leaves them alone.
export function encodeKey(key) {
  return key
    .split("/")
    .map((segment) =>
      encodeURIComponent(segment).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
    )
    .join("/");
}

// Uploaded files are served from the UI's own origin, so anything that can carry
// script (HTML, SVG, XML) must never be rendered inline.
const INLINE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"];

export function dispositionType(contentType, forceDownload) {
  const type = (contentType || "").split(";")[0].trim().toLowerCase();
  return !forceDownload && INLINE_TYPES.includes(type) ? "inline" : "attachment";
}
