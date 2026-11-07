import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { VoiceBubble } from "./VoiceBubble";
import type { Item } from "../types";

type VoiceItem = Extract<Item, { kind: "voice" }>;

const ITEM: VoiceItem = {
  kind: "voice",
  audio: "data:audio/mpeg;base64,AAAA",
  text: "这是语音条对应的正文。",
  seconds: 4.2,
};

/** jsdom 不会真的解码音频：用假 Audio 记录 src / play / pause，验证交互契约。 */
class FakeAudio {
  static instances: FakeAudio[] = [];
  src: string;
  duration = 4.2;
  paused = true;
  onloadedmetadata: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  play = vi.fn(async () => {
    this.paused = false;
  });
  pause = vi.fn(() => {
    this.paused = true;
  });
  constructor(src: string) {
    this.src = src;
    FakeAudio.instances.push(this);
  }
}

beforeEach(() => {
  FakeAudio.instances = [];
  vi.stubGlobal("Audio", FakeAudio);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("VoiceBubble", () => {
  it("默认静默：只显示时长，不创建也不播放音频", () => {
    render(<VoiceBubble item={ITEM} />);
    expect(screen.getByText("4″")).toBeTruthy();
    expect(FakeAudio.instances.length).toBe(0);
    expect(screen.getByRole("button").getAttribute("data-playing")).toBe("false");
  });

  it("点一下才播：用 data URL 构造 Audio 并 play", () => {
    render(<VoiceBubble item={ITEM} />);
    fireEvent.click(screen.getByRole("button"));
    expect(FakeAudio.instances.length).toBe(1);
    const audio = FakeAudio.instances[0];
    expect(audio.src).toBe(ITEM.audio);
    expect(audio.play).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button").getAttribute("data-playing")).toBe("true");
  });

  it("再点一下停止播放", () => {
    render(<VoiceBubble item={ITEM} />);
    const button = screen.getByRole("button");
    fireEvent.click(button);
    fireEvent.click(button);
    expect(FakeAudio.instances[0].pause).toHaveBeenCalledTimes(1);
    expect(button.getAttribute("data-playing")).toBe("false");
  });

  it("播完自动复位，不会卡在「正在播」", () => {
    render(<VoiceBubble item={ITEM} />);
    const button = screen.getByRole("button");
    fireEvent.click(button);
    act(() => {
      FakeAudio.instances[0].onended?.();
    });
    expect(button.getAttribute("data-playing")).toBe("false");
  });

  it("卸载时强制停掉，避免切会话后声音还在响", () => {
    const { unmount } = render(<VoiceBubble item={ITEM} />);
    fireEvent.click(screen.getByRole("button"));
    const audio = FakeAudio.instances[0];
    unmount();
    expect(audio.pause).toHaveBeenCalled();
  });

  it("时长决定气泡宽度（微信式：越长越宽，但有上限）", () => {
    const { rerender } = render(<VoiceBubble item={{ ...ITEM, seconds: 2 }} />);
    const narrow = Number.parseFloat(
      (screen.getByRole("button") as HTMLElement).style.width,
    );
    rerender(<VoiceBubble item={{ ...ITEM, seconds: 30 }} />);
    const wide = Number.parseFloat(
      (screen.getByRole("button") as HTMLElement).style.width,
    );
    expect(wide).toBeGreaterThan(narrow);
    expect(wide).toBeLessThanOrEqual(232);
  });

  it("截断的消息给出提示", () => {
    const { container } = render(<VoiceBubble item={{ ...ITEM, truncated: true }} />);
    // 断言结构不断言文案：测试环境的 i18n 语言不确定（en fallback），锁文案会假红。
    expect(container.querySelector(".voice-truncated")).toBeTruthy();
  });

  it("正文挂到 title 上，鼠标悬停能看原话", () => {
    render(<VoiceBubble item={ITEM} />);
    expect(screen.getByRole("button").getAttribute("title")).toBe(ITEM.text);
  });
});
