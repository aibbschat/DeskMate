/**
 * mascotEmoteMap 单测：激素→表情 / 状态→表情 / 最终解析优先级。
 */

import { describe, expect, it } from "vitest";
import {
  asExpressionId,
  faceToMascotState,
  hormoneToExpression,
  isWorkingState,
  mascotStateToExpression,
  resolveFace,
  type HormoneState,
} from "./mascotEmoteMap";

function h(over: Partial<HormoneState>): HormoneState {
  return { dopamine: 30, serotonin: 30, oxytocin: 30, cortisol: 30, ...over };
}

describe("hormoneToExpression（激素持续心情层）", () => {
  it("开心：dopamine 高 + cortisol 低 → laugh", () => {
    expect(hormoneToExpression(h({ dopamine: 80, cortisol: 20 }))).toBe("laugh");
  });

  it("压力主导 + 兴奋（dopamine 也高）→ angry", () => {
    expect(hormoneToExpression(h({ dopamine: 80, cortisol: 70 }))).toBe("angry");
  });

  it("压力主导 + 低落（dopamine 低）→ cry", () => {
    expect(hormoneToExpression(h({ dopamine: 30, cortisol: 70 }))).toBe("cry");
  });

  it("平静：serotonin 高且 dopamine 不高 → sleepy", () => {
    expect(hormoneToExpression(h({ serotonin: 75, dopamine: 30 }))).toBe("sleepy");
  });

  it("平静但 dopamine 也高时仍优先开心 → laugh", () => {
    expect(hormoneToExpression(h({ serotonin: 75, dopamine: 70 }))).toBe("laugh");
  });

  it("亲近温暖：oxytocin 高 → shy", () => {
    expect(hormoneToExpression(h({ oxytocin: 70 }))).toBe("shy");
  });

  it("默认（激素都在阈值内）→ smile", () => {
    expect(hormoneToExpression(h({}))).toBe("smile");
  });
});

describe("mascotStateToExpression（瞬时/事件层）", () => {
  it("工作类状态 → neutral", () => {
    for (const s of ["thinking", "working", "reading", "searching"] as const) {
      expect(mascotStateToExpression(s)).toBe("neutral");
    }
  });

  it("happy → cheer", () => {
    expect(mascotStateToExpression("happy")).toBe("cheer");
  });

  it("concerned → neutral（UI 状态不劫持情绪脸，哭只来自激素 cortisol>=60）", () => {
    expect(mascotStateToExpression("concerned")).toBe("neutral");
  });

  it("idle / walking / null → smile（不覆盖激素层）", () => {
    expect(mascotStateToExpression("idle")).toBe("smile");
    expect(mascotStateToExpression("walking")).toBe("smile");
    expect(mascotStateToExpression(null)).toBe("smile");
  });
});

describe("isWorkingState", () => {
  it("只认四种工作状态", () => {
    expect(isWorkingState("thinking")).toBe(true);
    expect(isWorkingState("idle")).toBe(false);
    expect(isWorkingState(null)).toBe(false);
  });
});

describe("resolveFace（最终优先级）", () => {
  it("激素缺失时退回运行状态映射", () => {
    expect(resolveFace(null, "happy")).toBe("cheer");
    expect(resolveFace(undefined, "concerned")).toBe("neutral");
    expect(resolveFace(null, "thinking")).toBe("neutral");
    expect(resolveFace(null, null)).toBe("smile");
  });

  it("瞬时工作状态优先于激素（思考时激素再开心也是中性脸）", () => {
    expect(resolveFace(h({ dopamine: 80, cortisol: 20 }), "thinking")).toBe("neutral");
    expect(resolveFace(h({ dopamine: 80, cortisol: 20 }), "working")).toBe("neutral");
  });

  it("非工作状态时由激素持续心情决定", () => {
    expect(resolveFace(h({ dopamine: 80, cortisol: 20 }), "idle")).toBe("laugh");
    expect(resolveFace(h({ cortisol: 70 }), "idle")).toBe("cry");
    expect(resolveFace(h({ oxytocin: 70 }), "walking")).toBe("shy");
  });
});

// ---------------------------------------------------------------------------
// 2026-10-12：阈值单一真相源移到后端。前端只消费 mood.face，不再自比激素阈值
// ——旧阈值(65/60/60/60)对 serotonin/oxytocin/cortisol 实际不可达，前后端两份
// 判定一起永远停在兜底 smile，是「看不见也听不到她情绪」的同一个根因。
// ---------------------------------------------------------------------------

describe("resolveFace 优先采用后端下发的 face", () => {
  it("后端 face 存在时压过本地激素阈值", () => {
    // 本地阈值算这组激素会得 smile（都在旧阈值内），但后端说她是亲近
    expect(resolveFace(h({ oxytocin: 45 }), "idle", "shy")).toBe("shy");
    expect(resolveFace(h({}), "idle", "cry")).toBe("cry");
  });

  it("瞬时工作状态仍优先于后端 face", () => {
    expect(resolveFace(h({}), "thinking", "laugh")).toBe("neutral");
    expect(resolveFace(h({}), "working", "cry")).toBe("neutral");
  });

  it("激素缺失但有后端 face 时照样能渲染", () => {
    expect(resolveFace(null, "idle", "shy")).toBe("shy");
  });

  it("后端 face 非法/缺失时回落本地激素阈值（兼容旧后端）", () => {
    expect(resolveFace(h({ dopamine: 80, cortisol: 20 }), "idle", null)).toBe("laugh");
    expect(resolveFace(h({ cortisol: 70 }), "idle", undefined)).toBe("cry");
  });
});

describe("asExpressionId 校验后端脏数据", () => {
  it("合法帧 id 原样返回", () => {
    expect(asExpressionId("laugh")).toBe("laugh");
    expect(asExpressionId("smile")).toBe("smile");
  });

  it("非法值一律 null，不致于把脸渲染成空", () => {
    expect(asExpressionId("explode")).toBeNull();
    expect(asExpressionId("")).toBeNull();
    expect(asExpressionId(undefined)).toBeNull();
    expect(asExpressionId(42)).toBeNull();
  });
});

describe("faceToMascotState（心情 → 姿态/粒子层）", () => {
  it("压力类 → concerned（低头后缩）", () => {
    expect(faceToMascotState("cry")).toBe("concerned");
    expect(faceToMascotState("angry")).toBe("concerned");
  });

  it("开心 → happy（弹跳）", () => {
    expect(faceToMascotState("laugh")).toBe("happy");
  });

  it("松弛 → idle", () => {
    expect(faceToMascotState("sleepy")).toBe("idle");
  });

  it("亲近/平淡/中性 → null，不覆盖既有姿态", () => {
    expect(faceToMascotState("shy")).toBeNull();
    expect(faceToMascotState("smile")).toBeNull();
    expect(faceToMascotState("neutral")).toBeNull();
    expect(faceToMascotState(null)).toBeNull();
  });
});
