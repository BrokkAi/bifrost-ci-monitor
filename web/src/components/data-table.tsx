import { useState } from "react";
import { plural } from "@/lib/utils";
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowUp,
  ChevronsUpDown,
  ChevronLeft,
  ChevronRight,
  Search,
} from "lucide-react";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { EmptyState } from "./common";
export function DataTable<T>({
  data,
  columns,
  searchPlaceholder,
  pageSize = 10,
}: {
  data: T[];
  columns: ColumnDef<T>[];
  searchPlaceholder?: string;
  pageSize?: number;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");
  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
  });
  return (
    <>
      {searchPlaceholder && (
        <div className="border-b p-4">
          <div className="relative max-w-sm">
            <Search
              size={14}
              className="absolute left-3 top-3 text-muted-foreground"
            />
            <Input
              aria-label={searchPlaceholder}
              value={globalFilter}
              onChange={(e) => setGlobalFilter(e.target.value)}
              placeholder={searchPlaceholder}
              className="pl-9"
            />
          </div>
        </div>
      )}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header) => (
                  <th key={header.id}>
                    {header.isPlaceholder ? null : header.column.getCanSort() ? (
                      <button
                        onClick={header.column.getToggleSortingHandler()}
                        className="flex items-center gap-2 whitespace-nowrap"
                      >
                        {flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )}
                        {header.column.getIsSorted() === "asc" ? (
                          <ArrowUp size={12} />
                        ) : header.column.getIsSorted() === "desc" ? (
                          <ArrowDown size={12} />
                        ) : (
                          <ChevronsUpDown size={11} className="opacity-45" />
                        )}
                      </button>
                    ) : (
                      flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!table.getFilteredRowModel().rows.length && (
        <EmptyState title="No matching entries">
          Try a different search or filter.
        </EmptyState>
      )}
      {table.getFilteredRowModel().rows.length > pageSize && (
        <div className="flex items-center justify-between border-t px-5 py-3 text-xs text-muted-foreground">
          <span>
            {plural(table.getFilteredRowModel().rows.length, "entry", "entries")} · Page{" "}
            {table.getState().pagination.pageIndex + 1} of{" "}
            {table.getPageCount()}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="icon"
              className="size-7"
              disabled={!table.getCanPreviousPage()}
              onClick={() => table.previousPage()}
              aria-label="Previous page"
            >
              <ChevronLeft />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="size-7"
              disabled={!table.getCanNextPage()}
              onClick={() => table.nextPage()}
              aria-label="Next page"
            >
              <ChevronRight />
            </Button>
          </div>
        </div>
      )}
    </>
  );
}
