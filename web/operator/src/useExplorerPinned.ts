import { useEffect, useState } from "react";

const STORAGE_KEY = "avalanche.explorer-pinned";

export function useExplorerPinned() {
  const [pinned, setPinned] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) !== "false";
    } catch {
      // Embedded/private browser contexts may disable storage.
      return true;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(pinned));
    } catch {
      // The sidebar remains usable for this session without persistence.
    }
  }, [pinned]);

  return [pinned, setPinned] as const;
}
