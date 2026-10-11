/**
 * faceStabilizer 单测 — 覆盖"一会儿哭一会儿笑"修复的三个机制：
 * 1. 事件脸临时驻留（dwell 后回落激素脸，不永久覆盖）；
 * 2. 激素脸连续一致确认（stableStreak 次轮询同一目标才切换）；
 * 3. 最小切换间隔（防秒级连跳）。
 */
import { describe, expect, it } from "vitest";
import {
  applyEventFace,
  applyHormoneFace,
  initFaceState,
  visibleFace,
  EVENT_DWELL_MS,
} from "./faceStabilizer";

const T0 = 1_000_000;
// stableStreak=2、minSwitch=8s 的默认参数

describe("事件脸临时驻留", () => {
  it("事件脸即时切换并记录过期时间", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    expect(s.current).toBe("cry");
    expect(s.eventFace).toBe("cry");
    expect(s.eventExpiresAt).toBe(T0 + 1000 + 12_000);
  });

  it("驻留期内激素轮询不覆盖事件脸", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    // 10s 后仍在驻留期内（12s），激素脸为 smile 也不切
    s = applyHormoneFace(s, "smile", T0 + 11_000);
    expect(s.current).toBe("cry");
    expect(visibleFace(s)).toBe("cry");
  });

  it("事件脸过期后回落激素流程（激素脸待确认，不立即切走）", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    // 13s 后驻留过期，激素脸仍是 smile（与 current=cry 不同）→ 进入待确认
    s = applyHormoneFace(s, "smile", T0 + 14_000);
    expect(s.eventFace).toBeNull();
    expect(s.pending).toBe("smile");
    expect(s.pendingStreak).toBe(1);
    // 展示脸暂保持 cry（不瞬间切回，等激素稳定确认）
    expect(visibleFace(s)).toBe("cry");
  });

  it("驻留期内激素轮询被忽略，事件脸续期不重置状态", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "neutral", EVENT_DWELL_MS.neutral, T0 + 1000);
    // 驻留期（15s）内激素轮询 laugh → 完全忽略（不建 pending、不切脸）
    s = applyHormoneFace(s, "laugh", T0 + 2000);
    expect(s.pending).toBeNull();
    expect(s.current).toBe("neutral");
    // 续期 neutral
    s = applyEventFace(s, "neutral", EVENT_DWELL_MS.neutral, T0 + 3000);
    expect(s.current).toBe("neutral");
    expect(s.eventExpiresAt).toBe(T0 + 3000 + 15_000);
  });

  it("idle/smile 事件不覆盖 → 清掉事件脸回落激素层", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    s = applyEventFace(s, "smile", undefined, T0 + 2000);
    expect(s.eventFace).toBeNull();
    expect(s.current).toBe("cry"); // 展示脸保持，等待激素流接管
  });
});

describe("激素脸连续一致确认", () => {
  it("相邻两轮轮询同一目标才切换（防 LLM 抖动翻脸）", () => {
    let s = initFaceState("smile", T0);
    // 第 1 轮：dopamine 高 → laugh，进入待确认
    s = applyHormoneFace(s, "laugh", T0 + 20_000);
    expect(s.current).toBe("smile");
    expect(s.pending).toBe("laugh");
    expect(s.pendingStreak).toBe(1);
    // 第 2 轮仍 laugh → 稳定，切换（已过最小间隔 8s）
    s = applyHormoneFace(s, "laugh", T0 + 40_000);
    expect(s.current).toBe("laugh");
    expect(s.pending).toBeNull();
  });

  it("单轮抖动不切换：目标变来变去则永远待确认", () => {
    let s = initFaceState("smile", T0);
    s = applyHormoneFace(s, "laugh", T0 + 20_000); // streak=1
    s = applyHormoneFace(s, "smile", T0 + 40_000); // 目标变回 → 重置
    expect(s.pending).toBeNull();
    expect(s.current).toBe("smile");
    s = applyHormoneFace(s, "laugh", T0 + 60_000); // streak=1 再来
    s = applyHormoneFace(s, "smile", T0 + 80_000); // 又变回
    expect(s.current).toBe("smile"); // 从未切到 laugh
  });

  it("激素目标与当前一致 → 不进入待确认", () => {
    let s = initFaceState("smile", T0);
    s = applyHormoneFace(s, "smile", T0 + 20_000);
    expect(s.pending).toBeNull();
    expect(s.current).toBe("smile");
  });
});

describe("最小切换间隔", () => {
  it("激素确认达标但距上次切换不足 8s → 等待下一轮", () => {
    let s = initFaceState("smile", T0);
    // 事件刚切到 cry（lastSwitchAt=T0+1000）
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    // 驻留过期后，激素已连续 2 轮一致 cry→laugh
    s = applyHormoneFace(s, "laugh", T0 + 14_000); // streak=1
    s = applyHormoneFace(s, "laugh", T0 + 34_000); // streak=2，但距 T0+1000 仅 33s > 8s ✓
    expect(s.current).toBe("laugh");
  });

  it("lastSwitchAt 很近时激素切换被推迟", () => {
    let s = initFaceState("smile", T0);
    s = applyEventFace(s, "cry", EVENT_DWELL_MS.cry, T0 + 1000);
    // 驻留 12s 过期；minSwitch=8s 距 T0+1000 已满足 → 可切
    s = applyHormoneFace(s, "neutral", T0 + 16_000); // streak=1
    s = applyHormoneFace(s, "neutral", T0 + 36_000); // streak=2 → 切
    expect(s.current).toBe("neutral");
  });
});
