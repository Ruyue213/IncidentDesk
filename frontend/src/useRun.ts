import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.ts';
import { EVENT_KINDS, isTerminal, mergeEvents, preferRun, readEventCache, writeStore } from './state.ts';
import type { BusinessEvent, Run } from './types.ts';

export type Connection = 'connecting' | 'live' | 'reconnecting' | 'complete' | 'idle';

export function useRun(id: string | null, onUpdate: (run: Run) => void) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<BusinessEvent[]>([]);
  const [connection, setConnection] = useState<Connection>('idle');
  const [error, setError] = useState('');
  const updateRef = useRef(onUpdate);
  updateRef.current = onUpdate;
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    setRun(null); setEvents([]); setError('');
    if (!id) { setConnection('idle'); refreshRef.current = async () => {}; return; }
    const runId = id;
    const abort = new AbortController();
    let stopped = false;
    let terminal = false;
    let terminalDrained = false;
    let draining = false;
    let source: EventSource | null = null;
    let retry: number | undefined;
    let poll: number | undefined;
    let connecting = false;
    let fetching: Promise<Run | null> | null = null;
    let latestRun: Run | undefined;
    let cache = readEventCache(localStorage, runId);
    setEvents(cache.events); setConnection('connecting');

    const acceptEvents = (incoming: BusinessEvent[]) => {
      if (stopped) return;
      cache = mergeEvents(cache, incoming, runId);
      // Persist cursor and visible event history in a single atomic storage write.
      writeStore(localStorage, `incidentdesk.events.${runId}`, cache);
      setEvents(cache.events);
    };
    const close = () => {
      source?.close(); source = null;
      window.clearTimeout(retry); window.clearInterval(poll);
    };
    const catchUp = async () => {
      while (!stopped) {
        const before = cache.cursor;
        const page = await api.events(runId, before, abort.signal);
        acceptEvents(page.items);
        if (page.items.length < 200 || cache.cursor <= before) return;
      }
    };
    const drainTerminal = async () => {
      if (stopped || draining || terminalDrained) return;
      draining = true;
      source?.close(); source = null;
      try {
        await catchUp();
        if (!stopped) {
          terminalDrained = true; close(); setConnection('complete');
        }
      } catch {
        if (!stopped) {
          setConnection('reconnecting'); window.clearTimeout(retry);
          retry = window.setTimeout(() => { void drainTerminal(); }, 1500);
        }
      } finally { draining = false; }
    };
    const refreshRun = (): Promise<Run | null> => {
      if (fetching) return fetching;
      fetching = api.run(runId, abort.signal).then(async (incoming) => {
        if (stopped) return null;
        const current = preferRun(latestRun, incoming);
        latestRun = current;
        setRun(current); updateRef.current(current); setError('');
        terminal = isTerminal(current.status);
        if (terminal) await drainTerminal();
        return current;
      }).catch((failure: unknown) => {
        if (!stopped) {
          setError(failure instanceof Error ? failure.message : '暂时无法读取任务');
          setConnection('reconnecting');
        }
        return null;
      }).finally(() => { fetching = null; });
      return fetching;
    };
    refreshRef.current = async () => { await refreshRun(); };
    const scheduleReconnect = () => {
      if (stopped) return;
      if (terminal) { void drainTerminal(); return; }
      source?.close(); source = null;
      window.clearTimeout(retry);
      setConnection('reconnecting');
      retry = window.setTimeout(() => { void connect(); }, 1500);
    };
    const connect = async () => {
      if (stopped || terminal || connecting) return;
      connecting = true;
      try {
        await catchUp();
        const current = await refreshRun();
        if (stopped || terminal || !current) { if (!stopped && !terminal) scheduleReconnect(); return; }
        source?.close();
        source = new EventSource(api.streamPath(runId, cache.cursor));
        source.onopen = () => { if (!stopped) setConnection('live'); };
        source.onerror = scheduleReconnect;
        for (const kind of EVENT_KINDS) {
          source.addEventListener(kind, (message) => {
            if (stopped) return;
            try {
              const event = JSON.parse((message as MessageEvent<string>).data) as BusinessEvent;
              acceptEvents([event]);
              void refreshRun();
            } catch { scheduleReconnect(); }
          });
        }
      } catch { scheduleReconnect(); }
      finally { connecting = false; }
    };
    poll = window.setInterval(() => { if (!terminal || !terminalDrained) void refreshRun(); }, 2500);
    void connect();
    return () => { stopped = true; close(); abort.abort(); };
  }, [id]);

  const refresh = useCallback(async () => { await refreshRef.current(); }, []);
  return { run, events, connection, error, refresh };
}
