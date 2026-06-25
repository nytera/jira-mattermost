import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";

interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Run an async fetcher on mount and on `deps` change, with manual reload.
 * 401s are swallowed here — the client's unauthorized handler already routes
 * the user back to login, so pages should not also render an error for them. */
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    fetcherRef.current()
      .then((result) => {
        if (active) setData(result);
      })
      .catch((err) => {
        if (!active) return;
        if (err instanceof ApiError && err.status === 401) return;
        setError(err instanceof Error ? err.message : "Не удалось загрузить данные.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, ...deps]);

  return { data, error, loading, reload };
}
