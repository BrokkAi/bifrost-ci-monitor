import { Button } from "./ui/button";
export function HistoryFooter({
  hasMore,
  loading,
  loaded,
  loadMore,
  error,
}: {
  hasMore: boolean;
  loading: boolean;
  loaded: number;
  loadMore: () => unknown;
  error?: Error | null;
}) {
  if (!hasMore && !error) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3">
      <div className="text-[10px] text-muted-foreground">
        <span>{loaded} loaded</span>
        {error && (
          <p role="alert" className="mt-1">
            Could not load history entries. Try again.
          </p>
        )}
      </div>
      <Button variant="outline" size="sm" onClick={loadMore} disabled={loading}>
        {loading ? "Loading…" : "Load more"}
      </Button>
    </div>
  );
}
