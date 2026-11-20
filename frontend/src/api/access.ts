/** The site's access code, remembered in localStorage ("archaeologist.access"). Storage can be
 *  unavailable or throw (private windows, blocked site data), so every access is guarded and
 *  the code is also kept in memory for the life of the page. */

export const ACCESS_KEY = "archaeologist.access";

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export interface AccessStore {
  get(): string | null;
  set(code: string): void;
  clear(): void;
  subscribe(fn: () => void): () => void;
}

export function createAccessStore(storage: () => StorageLike | undefined | null): AccessStore {
  let memory: string | null = null;
  let loaded = false;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((fn) => fn());
  const read = (): string | null => {
    try {
      const s = storage();
      if (s) return s.getItem(ACCESS_KEY);
    } catch {
      /* fall through to memory */
    }
    return null;
  };
  return {
    get() {
      if (!loaded) {
        loaded = true;
        memory = read() || null;
      }
      return memory;
    },
    set(code) {
      memory = code;
      loaded = true;
      try {
        storage()?.setItem(ACCESS_KEY, code);
      } catch {
        /* kept in memory only */
      }
      emit();
    },
    clear() {
      memory = null;
      loaded = true;
      try {
        storage()?.removeItem(ACCESS_KEY);
      } catch {
        /* nothing stored */
      }
      emit();
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export const access = createAccessStore(() =>
  typeof window === "undefined" ? undefined : window.localStorage,
);
