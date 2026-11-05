/**
 * petClipMap 单测：表情/情绪状态 → 序列帧 clip 名。
 *
 * 为什么需要这层测试（2026-11-05）：
 *   clip 选择原先内联在 XiaojuMascot 的 IIFE 里，没有任何测试。九段素材
 *   （idle/working/thinking/happy/cry/angry/shock/sleepy/shy）一旦某条分支写错，
 *   表现是「某种情绪下她一直站着呼吸」——不报错、不崩溃，肉眼也很难确认是不是
 *   素材坏了。这里把九条映射逐条锁死。
 *
 * 特别锁住 TWO 个易错点：
 *   1. 工作状态下 resolveFace 给的是 "neutral"（isWorkingState 优先），
 *      clip 只能靠 emotion=thinking/focused 反推，不能只认 expression；
 *   2. 「走路」没有独立素材，必须回落 idle，且不能吞掉具名情绪。
 */
import { describe, expect, it } from "vitest";
import { PET_CLIP_NAMES, resolveClip, type PetClipName } from "./petClipMap";
import type { ExpressionId } from "./mascotEmoteMap";

describe("resolveClip：九种表情各自命中对应素材", () => {
  const cases: Array<[ExpressionId, PetClipName]> = [
    ["cry", "cry"],
    ["laugh", "happy"],
    ["cheer", "happy"],
    ["angry", "angry"],
    ["shock", "shock"],
    ["sleepy", "sleepy"],
    ["shy", "shy"],
  ];

  it.each(cases)("expression=%s → %s", (expression, expected) => {
    expect(resolveClip(expression, "idle", false)).toBe(expected);
  });

  it("smile / neutral 无专属素材，回落 idle（常态呼吸）", () => {
    expect(resolveClip("smile", "idle", false)).toBe("idle");
    expect(resolveClip("neutral", "idle", false)).toBe("idle");
    expect(resolveClip(undefined, "idle", false)).toBe("idle");
    expect(resolveClip(null, "idle", false)).toBe("idle");
  });
});

describe("resolveClip：工作状态靠 emotion 反推（expression 此时是 neutral）", () => {
  it("thinking 状态 → thinking 素材（托下巴想办法）", () => {
    expect(resolveClip("neutral", "thinking", false)).toBe("thinking");
  });

  it("focused 状态 → working 素材（低头做事），不是 thinking", () => {
    // focused 覆盖 working / reading / searching，都是「在做事」
    expect(resolveClip("neutral", "focused", false)).toBe("working");
  });

  it("具名表情优先于工作状态（后端下发情绪脸时不被工作态吞掉）", () => {
    expect(resolveClip("cry", "focused", false)).toBe("cry");
    expect(resolveClip("angry", "thinking", false)).toBe("angry");
  });
});

describe("resolveClip：走路回落 idle，且不吞具名情绪", () => {
  it("走路且无具名表情 → idle", () => {
    expect(resolveClip("smile", "idle", true)).toBe("idle");
    expect(resolveClip(undefined, null, true)).toBe("idle");
  });

  it("走路时有具名情绪 → 仍播情绪素材（不能因为走路就变常态脸）", () => {
    expect(resolveClip("cry", "idle", true)).toBe("cry");
  });
});

describe("PET_CLIP_NAMES：与 manifest / 磁盘素材对齐", () => {
  it("恰好九段，无多余、无遗漏", () => {
    expect([...PET_CLIP_NAMES].sort()).toEqual(
      ["angry", "cry", "happy", "idle", "shock", "shy", "sleepy", "thinking", "working"],
    );
  });

  it("resolveClip 只会返回这九段之一", () => {
    const expressions: Array<ExpressionId | undefined> = [
      "smile", "laugh", "cheer", "cry", "angry", "shock", "sleepy", "shy", "neutral", undefined,
    ];
    const emotions = ["idle", "thinking", "happy", "concerned", "focused", null] as const;
    for (const e of expressions) {
      for (const m of emotions) {
        for (const w of [true, false]) {
          expect(PET_CLIP_NAMES).toContain(resolveClip(e, m, w));
        }
      }
    }
  });
});
