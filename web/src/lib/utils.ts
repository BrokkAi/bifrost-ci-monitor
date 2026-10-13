import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
export const display = (s: string | null | undefined) =>
  s ? s.replaceAll("_", " ").replaceAll("-", " ") : "—";
const pluralRules = new Intl.PluralRules("en");
/** "1 pull request", "2 pull requests"; pass `many` for irregular forms or verb agreement. */
export function plural(count: number, one: string, many = `${one}s`) {
  return `${count.toLocaleString("en")} ${pluralRules.select(count) === "one" ? one : many}`;
}
/** Release-branch refs are generated IDs; show them as "release branch" and keep the ref for hover text. */
export function targetLabel(ref: string | null | undefined) {
  if (!ref || ref === "master") return "master";
  return ref.startsWith("stabilize/") ? "release branch" : ref;
}
export function duration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}h ${m}m` : `${m}m ${s}s`;
}
export const durationFromMinutes = (value: number | null | undefined) =>
  value == null ? "—" : duration(value * 60);

export function sentenceCase(value: string | null | undefined) {
  const words = display(value);
  return words[0].toUpperCase() + words.slice(1);
}
export function absoluteTime(value: string | null | undefined) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
