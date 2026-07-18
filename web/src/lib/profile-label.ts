export function formatProfileLabel(name: string) {
  const trimmed = (name || "default").trim() || "default";
  return trimmed
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("-");
}