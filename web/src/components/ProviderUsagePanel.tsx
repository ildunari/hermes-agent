import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Gauge,
  RefreshCw,
  X,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  SubscriptionUsageProvider,
  SubscriptionUsageResponse,
  SubscriptionUsageWindow,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type LoadState = "idle" | "loading" | "ready" | "error";

const WINDOW_KEYS = [
  ["primary", "Primary"],
  ["secondary", "Secondary"],
  ["tertiary", "Tertiary"],
] as const;

function providerName(provider: SubscriptionUsageProvider): string {
  const usage = provider.usage;
  return (
    provider.provider ||
    usage?.identity?.providerID ||
    usage?.identity?.providerId ||
    usage?.identity?.provider ||
    "unknown"
  );
}

function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function numericPercent(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const n = Number(value.replace("%", ""));
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function windowPercent(win: SubscriptionUsageWindow): number | null {
  return numericPercent(win.usedPercent ?? win.used_percent);
}

function formatPercent(value: number | null): string {
  if (value === null) return "unknown";
  return `${Math.max(0, Math.min(100, Math.round(value)))}%`;
}

function formatReset(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function providerError(provider: SubscriptionUsageProvider): string {
  const err = provider.error;
  if (!err) return "No usage data";
  if (typeof err === "string") return err;
  return err.message || "No usage data";
}

function usageWindows(provider: SubscriptionUsageProvider) {
  const usage = provider.usage;
  if (!usage) return [];

  const windows = WINDOW_KEYS.flatMap(([key, fallbackLabel]) => {
    const win = usage[key];
    if (!win) return [];
    return [{ label: win.label || fallbackLabel, win }];
  });

  if (Array.isArray(usage.windows)) {
    windows.push(
      ...usage.windows.map((win, index) => ({
        label: win.label || `Window ${index + 1}`,
        win,
      })),
    );
  } else if (usage.windows && typeof usage.windows === "object") {
    windows.push(
      ...Object.entries(usage.windows).map(([label, win]) => ({
        label: win.label || titleCase(label),
        win,
      })),
    );
  }

  return windows;
}

function ProviderCard({ provider }: { provider: SubscriptionUsageProvider }) {
  const windows = usageWindows(provider);
  const name = providerName(provider);
  const usage = provider.usage;

  if (!usage) {
    return (
      <div className="border border-current/10 bg-background-base/35 p-3">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <div className="min-w-0">
            <div className="font-expanded text-[0.75rem] font-bold tracking-[0.08em] text-midground">
              {titleCase(name)}
            </div>
            <p className="mt-1 line-clamp-3 font-mono-ui text-[0.68rem] normal-case leading-snug text-muted-foreground">
              {providerError(provider)}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="border border-current/15 bg-card/70 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-expanded text-[0.78rem] font-bold tracking-[0.08em] text-midground">
            {titleCase(name)}
          </div>
          <div className="mt-0.5 font-mono-ui text-[0.66rem] normal-case text-muted-foreground">
            {usage.plan || usage.title || "Provider usage"}
          </div>
        </div>
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
      </div>

      <div className="mt-3 space-y-2.5">
        {windows.length > 0 ? (
          windows.map(({ label, win }, index) => {
            const percent = windowPercent(win);
            const clamped = Math.max(0, Math.min(100, percent ?? 0));
            const reset = formatReset(
              win.resetAt ?? win.reset_at ?? win.resetsAt ?? win.resets_at,
            );
            return (
              <div key={`${label}-${index}`} className="space-y-1">
                <div className="flex items-center justify-between gap-3 font-mono-ui text-[0.66rem] normal-case text-muted-foreground">
                  <span className="truncate">{label}</span>
                  <span className="shrink-0 text-midground/85">
                    {formatPercent(percent)} used
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden bg-midground/10">
                  <div
                    className={cn(
                      "h-full transition-[width]",
                      clamped >= 90
                        ? "bg-destructive"
                        : clamped >= 70
                          ? "bg-warning"
                          : "bg-success",
                    )}
                    style={{ width: `${clamped}%` }}
                  />
                </div>
                {(reset || win.detail) && (
                  <div className="font-mono-ui text-[0.62rem] normal-case leading-snug text-muted-foreground/80">
                    {reset ? `resets ${reset}` : win.detail}
                  </div>
                )}
              </div>
            );
          })
        ) : (
          <div className="font-mono-ui text-[0.68rem] normal-case text-muted-foreground">
            Usage returned without window details.
          </div>
        )}
      </div>
    </div>
  );
}

export function ProviderUsagePanel({
  onClose,
  open,
}: {
  onClose: () => void;
  open: boolean;
}) {
  const [state, setState] = useState<LoadState>("idle");
  const [data, setData] = useState<SubscriptionUsageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      const response = await api.getSubscriptionUsage("all");
      setData(response);
      setState("ready");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setState("error");
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [load, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, open]);

  const { usable, unavailable } = useMemo(() => {
    const providers = data?.providers ?? [];
    return {
      usable: providers.filter((item) => item.usage),
      unavailable: providers.filter((item) => !item.usage),
    };
  }, [data]);

  return (
    <>
      <Button
        ghost
        aria-label="Close provider usage"
        className={cn(
          "fixed inset-0 z-50 block bg-black/40 p-0 backdrop-blur-[1px] lg:hidden",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
      />

      <aside
        aria-label="Provider usage"
        aria-hidden={!open}
        className={cn(
          "fixed bottom-0 right-0 top-0 z-50 flex w-full max-w-[25rem] flex-col",
          "border-l border-current/20 bg-background-base/95 text-midground shadow-2xl backdrop-blur-sm",
          "transition-transform duration-200 ease-out",
          open ? "translate-x-0" : "translate-x-full",
        )}
        style={{
          background: "var(--component-sidebar-background)",
          clipPath: "var(--component-sidebar-clip-path)",
          borderImage: "var(--component-sidebar-border-image)",
        }}
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-current/20 px-4">
          <div className="flex min-w-0 items-center gap-2">
            <Gauge className="h-4 w-4 shrink-0" />
            <div className="min-w-0">
              <div className="font-expanded text-[0.85rem] font-bold tracking-[0.08em]">
                Provider Usage
              </div>
              <div className="font-mono-ui text-[0.62rem] normal-case text-muted-foreground">
                CodexBar subscription windows
              </div>
            </div>
          </div>
          <Button
            ghost
            size="icon"
            aria-label="Close provider usage"
            onClick={onClose}
            className="text-midground/70 hover:text-midground"
          >
            <X />
          </Button>
        </div>

        <div className="flex items-center justify-between gap-3 border-b border-current/10 px-4 py-2">
          <div className="font-mono-ui text-[0.66rem] normal-case text-muted-foreground">
            {data?.updatedAt
              ? `updated ${formatReset(data.updatedAt)}`
              : "provider=all"}
          </div>
          <Button
            ghost
            size="sm"
            onClick={() => void load()}
            disabled={state === "loading"}
            className="gap-1.5 text-[0.68rem] tracking-[0.1em]"
          >
            {state === "loading" ? (
              <Spinner className="h-3.5 w-3.5" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            Refresh
          </Button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {state === "loading" && (
            <div className="flex items-center gap-2 border border-current/10 bg-card/60 p-3 font-mono-ui text-[0.72rem] normal-case text-muted-foreground">
              <Spinner className="h-3.5 w-3.5" />
              Fetching provider usage…
            </div>
          )}

          {state === "error" && (
            <div className="border border-destructive/40 bg-destructive/10 p-3">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                <div className="font-mono-ui text-[0.72rem] normal-case leading-snug">
                  {error || "Failed to load provider usage."}
                </div>
              </div>
            </div>
          )}

          {state === "ready" && data?.error && (
            <div className="border border-warning/40 bg-warning/10 p-3 font-mono-ui text-[0.72rem] normal-case leading-snug">
              {data.error}
            </div>
          )}

          {state === "ready" && usable.length === 0 && !data?.error && (
            <div className="border border-current/10 bg-card/60 p-3 font-mono-ui text-[0.72rem] normal-case text-muted-foreground">
              No working provider usage windows returned data.
            </div>
          )}

          {usable.map((provider, index) => (
            <ProviderCard key={`${providerName(provider)}-${index}`} provider={provider} />
          ))}

          {unavailable.length > 0 && (
            <details className="group border border-current/10 bg-background-base/25 p-3">
              <summary className="cursor-pointer list-none font-mono-ui text-[0.68rem] normal-case text-muted-foreground">
                {unavailable.length} unavailable collectors
                <span className="ml-1 text-midground/70 group-open:hidden">
                  · show
                </span>
                <span className="ml-1 hidden text-midground/70 group-open:inline">
                  · hide
                </span>
              </summary>
              <div className="mt-3 space-y-2">
                {unavailable.map((provider, index) => (
                  <ProviderCard
                    key={`${providerName(provider)}-error-${index}`}
                    provider={provider}
                  />
                ))}
              </div>
            </details>
          )}
        </div>

        <div className="shrink-0 border-t border-current/10 px-4 py-2 font-mono-ui text-[0.62rem] normal-case text-muted-foreground">
          Source: {data?.source || "codexbar"}
          {typeof data?.durationMs === "number" ? ` · ${data.durationMs}ms` : ""}
        </div>
      </aside>
    </>
  );
}
