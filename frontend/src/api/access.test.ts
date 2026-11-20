import { describe, expect, it, vi } from "vitest";
import { ACCESS_KEY, createAccessStore } from "./access";

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

const throwing = {
  getItem: () => {
    throw new Error("SecurityError");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
  removeItem: () => {
    throw new Error("SecurityError");
  },
};

describe("access store", () => {
  it("persists to storage", () => {
    const s = memoryStorage();
    const store = createAccessStore(() => s);
    expect(store.get()).toBeNull();
    store.set("open sesame");
    expect(s.data.get(ACCESS_KEY)).toBe("open sesame");
    expect(createAccessStore(() => s).get()).toBe("open sesame"); // a new page load reads it back
    store.clear();
    expect(store.get()).toBeNull();
    expect(s.data.has(ACCESS_KEY)).toBe(false);
  });

  it("falls back to memory when storage throws", () => {
    const store = createAccessStore(() => throwing);
    expect(store.get()).toBeNull();
    store.set("abc");
    expect(store.get()).toBe("abc");
    store.clear();
    expect(store.get()).toBeNull();
  });

  it("falls back to memory when storage is missing or its getter throws", () => {
    const none = createAccessStore(() => undefined);
    none.set("x");
    expect(none.get()).toBe("x");
    const broken = createAccessStore(() => {
      throw new Error("no window");
    });
    expect(broken.get()).toBeNull();
    broken.set("y");
    expect(broken.get()).toBe("y");
  });

  it("notifies subscribers on change", () => {
    const store = createAccessStore(() => memoryStorage());
    const fn = vi.fn();
    const off = store.subscribe(fn);
    store.set("a");
    store.clear();
    off();
    store.set("b");
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
