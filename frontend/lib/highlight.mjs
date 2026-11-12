// The backend's `highlight` is a sentence copied out of `snippet`. Returns the three parts to render
// as text around a <mark>, or null when there is nothing to mark.
export function splitHighlight(snippet, highlight) {
  const at = highlight && snippet ? snippet.indexOf(highlight) : -1;
  if (at < 0) return null;
  return { before: snippet.slice(0, at), match: highlight, after: snippet.slice(at + highlight.length) };
}
