import {
  type SetStateAction,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

export const DEFAULT_LAYOUT_STORAGE_KEY = "avalanche-operator-ui.layout";
export const INSPECTOR_MIN_WIDTH = 320;
export const INSPECTOR_MAX_WIDTH = 640;
const INSPECTOR_DEFAULT_WIDTH = 410;
export const LOG_PANE_MIN_HEIGHT_PX = 140;
export const LOG_PANE_DEFAULT_HEIGHT_PX = 260;

export interface WorkspaceLayout {
  inspectorWidth: number;
  timelineExpanded: boolean;
  logHeight: number;
  logExpanded: boolean;
}

export function maximumInspectorWidth() {
  return typeof window === "undefined"
    ? INSPECTOR_MAX_WIDTH
    : Math.max(INSPECTOR_MIN_WIDTH, Math.min(INSPECTOR_MAX_WIDTH, window.innerWidth));
}

export function maximumPaneHeight(element: HTMLElement | null = null) {
  const parentHeight = element?.parentElement?.clientHeight ?? 0;
  const viewportHeight = typeof window === "undefined" ? 960 : window.innerHeight;
  const availableHeight = parentHeight > 0 ? parentHeight : viewportHeight;
  return Math.max(LOG_PANE_MIN_HEIGHT_PX, Math.floor(availableHeight * 0.75));
}

export function boundedPaneHeight(height: number, maximum: number) {
  return Math.min(maximum, Math.max(LOG_PANE_MIN_HEIGHT_PX, height));
}

function boundedLayout(value: Partial<WorkspaceLayout> | null): WorkspaceLayout {
  const width = value?.inspectorWidth;
  const height = value?.logHeight;
  return {
    inspectorWidth: Math.min(
      maximumInspectorWidth(),
      Math.max(
        INSPECTOR_MIN_WIDTH,
        typeof width === "number" && Number.isFinite(width) ? width : INSPECTOR_DEFAULT_WIDTH,
      ),
    ),
    timelineExpanded:
      typeof value?.timelineExpanded === "boolean" ? value.timelineExpanded : false,
    logHeight: boundedPaneHeight(
      typeof height === "number" && Number.isFinite(height)
        ? height
        : LOG_PANE_DEFAULT_HEIGHT_PX,
      maximumPaneHeight(),
    ),
    logExpanded: typeof value?.logExpanded === "boolean" ? value.logExpanded : true,
  };
}

// A keyed route can render its replacement before the old workspace unmounts.
// Keep only unflushed committed values here so that replacement sees the latest
// drag immediately, without defeating the storage-write debounce.
const pendingLayouts = new Map<string, { owner: symbol; layout: WorkspaceLayout }>();

function readLayout(key: string): WorkspaceLayout {
  const pending = pendingLayouts.get(key);
  if (pending) return boundedLayout(pending.layout);
  try {
    return boundedLayout(JSON.parse(window.localStorage.getItem(key) ?? "null"));
  } catch {
    // SSR, embedded/private contexts and corrupt storage use the normal defaults.
    return boundedLayout(null);
  }
}

function writeLayout(key: string, layout: WorkspaceLayout) {
  try {
    window.localStorage.setItem(key, JSON.stringify(layout));
  } catch {
    // Layout remains usable in this session when storage is unavailable/full.
  }
}

export function useWorkspaceLayout(storageKey = DEFAULT_LAYOUT_STORAGE_KEY) {
  const [layout, setLayout] = useState(() => readLayout(storageKey));
  const owner = useRef(Symbol());
  const latest = useRef(layout);
  useLayoutEffect(() => {
    latest.current = layout;
    pendingLayouts.set(storageKey, { owner: owner.current, layout });
  }, [layout, storageKey]);
  useEffect(() => {
    const timeout = setTimeout(() => {
      writeLayout(storageKey, layout);
      if (pendingLayouts.get(storageKey)?.owner === owner.current)
        pendingLayouts.delete(storageKey);
    }, 150);
    return () => clearTimeout(timeout);
  }, [layout, storageKey]);
  useLayoutEffect(
    () => () => {
      writeLayout(storageKey, latest.current);
      if (pendingLayouts.get(storageKey)?.owner === owner.current)
        pendingLayouts.delete(storageKey);
    },
    [storageKey],
  );

  const setField = useCallback(
    <K extends keyof WorkspaceLayout>(field: K, value: SetStateAction<WorkspaceLayout[K]>) => {
      setLayout((current) => {
        const next = typeof value === "function" ? value(current[field]) : value;
        return Object.is(current[field], next) ? current : { ...current, [field]: next };
      });
    },
    [],
  );
  const setInspectorWidth = useCallback(
    (value: number) => setField("inspectorWidth", value),
    [setField],
  );
  const setTimelineExpanded = useCallback(
    (value: SetStateAction<boolean>) => setField("timelineExpanded", value),
    [setField],
  );
  const setLogHeight = useCallback(
    (value: SetStateAction<number>) => setField("logHeight", value),
    [setField],
  );
  const setLogExpanded = useCallback(
    (value: SetStateAction<boolean>) => setField("logExpanded", value),
    [setField],
  );
  return { layout, setInspectorWidth, setTimelineExpanded, setLogHeight, setLogExpanded };
}
