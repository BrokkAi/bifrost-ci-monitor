import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
export const display = (s: string | null | undefined) =>
  s ? s.replaceAll("_", " ").replaceAll("-", " ") : "—";
export function minutes(seconds: number | null | undefined) {
  return seconds == null ? "—" : `${Math.round(seconds / 60)} min`;
}

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
