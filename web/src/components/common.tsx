import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Check, Copy, Inbox, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { absoluteTime } from "@/lib/utils";
import { Button } from "./ui/button";
import { Card, CardContent } from "./ui/card";
export function RelativeTime({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  const abs = Math.abs(seconds);
  const [number, unit]: [number, Intl.RelativeTimeFormatUnit] =
    abs < 60
      ? [Math.round(seconds), "second"]
      : abs < 3600
        ? [Math.round(seconds / 60), "minute"]
        : abs < 86400
          ? [Math.round(seconds / 3600), "hour"]
          : [Math.round(seconds / 86400), "day"];
  return (
    <time dateTime={value} title={absoluteTime(value)}>
      {new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(
        -number,
        unit,
      )}
    </time>
  );
}
export function Sha({ value }: { value: string | null | undefined }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="text-muted-foreground">—</span>;
  return (
    <button
      className="sha"
      title={`Copy ${value}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        } catch {
          toast.error("Could not copy SHA. Select it from the hover text.");
        }
      }}
    >
      <code>{value.slice(0, 7)}</code>
      {copied ? <Check size={11} /> : <Copy size={11} />}
    </button>
  );
}
export function ExternalLink({
  href,
  children,
  className = "",
}: {
  href: string | null | undefined;
  children: ReactNode;
  className?: string;
}) {
  if (!href) return <span>{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className={`external-link ${className}`}
    >
      {children}
      <ArrowUpRight size={12} />
    </a>
  );
}
export function PageHeading({
  title,
  description,
  action,
}: {
  title: string;
  description: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}
export function Loading({ label = "Loading dashboard" }: { label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-4">
      <div className="h-8 w-52 animate-pulse rounded bg-muted" />
      <div className="grid gap-4 md:grid-cols-3">
        {[1, 2, 3].map((n) => (
          <div
            key={n}
            className="h-44 animate-pulse rounded-xl border bg-muted/50"
          />
        ))}
      </div>
    </div>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: Error;
  retry?: () => void;
}) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
        <Inbox className="text-muted-foreground" size={28} />
        <h2 className="font-semibold">Unable to load this view</h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {error.message}
        </p>
        {retry && (
          <Button variant="outline" onClick={retry}>
            <RefreshCw />
            Try again
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      <Inbox size={24} className="mb-2 text-muted-foreground" />
      <h3 className="font-medium text-sm">{title}</h3>
      {children && <p className="text-xs text-muted-foreground">{children}</p>}
    </div>
  );
}
export function SectionLink({
  to,
  children,
}: {
  to: string;
  children: ReactNode;
}) {
  return (
    <Link
      to={to}
      className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
    >
      {children}
      <ArrowUpRight size={13} />
    </Link>
  );
}
