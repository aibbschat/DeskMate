import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getTextSize, initTextSize, setTextSize } from "./textSize";

describe("textSize", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.textSize;
  });
  afterEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.textSize;
  });

  it("defaults to the factory size when nothing is stored", () => {
    expect(getTextSize()).toBe("default");
  });

  it("applies 小/大 as data-text-size and clears it for default", () => {
    setTextSize("large");
    expect(document.documentElement.dataset.textSize).toBe("large");
    expect(localStorage.getItem("openwork-text-size")).toBe("large");

    setTextSize("small");
    expect(document.documentElement.dataset.textSize).toBe("small");

    // default 必须删掉属性而不是写 "default"：styles.css 的 :root 默认值才生效
    setTextSize("default");
    expect(document.documentElement.dataset.textSize).toBeUndefined();
    expect(localStorage.getItem("openwork-text-size")).toBeNull();
  });

  it("ignores a corrupt stored value instead of throwing", () => {
    localStorage.setItem("openwork-text-size", "gigantic");
    expect(getTextSize()).toBe("default");
    expect(() => initTextSize()).not.toThrow();
    expect(document.documentElement.dataset.textSize).toBeUndefined();
  });

  it("broadcasts a change so the settings control stays in sync", () => {
    const spy = vi.fn();
    window.addEventListener("openwork:text-size", spy);
    setTextSize("small");
    window.removeEventListener("openwork:text-size", spy);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
