import { describe, expect, it } from "vitest";
import { FOLD_LABEL, shouldFold } from "./sensitive";

describe("手机端展示层敏感折叠（D3）", () => {
  it("家人是硬线：双字称谓命中", () => {
    for (const t of [
      "妈妈上周体检有两项要复查",
      "萧盛提到父亲年轻时的事",
      "女儿放学晚，他去接",
    ]) {
      const v = shouldFold(t);
      expect(v.fold, t).toBe(true);
      expect(["family", "health"]).toContain(v.reason);
    }
  });

  it("单字「家/书/信」绝不误伤（词表只收双字以上）", () => {
    for (const t of [
      "他把家书寄回镇上",
      "国子监的家世背景写法",
      "这封信是程啸写的",
      "小说里的书信体段落",
    ]) {
      expect(shouldFold(t).fold, t).toBe(false);
    }
  });

  it("手机号 / 18 位证件号形态按号码折", () => {
    expect(shouldFold("他的号码是 13812345678").reason).toBe("id");
    expect(shouldFold("身份证 430102199001011234").reason).toBe("id");
  });

  it("后端写入时留的显式标签一定折", () => {
    expect(shouldFold("某条事实 #private").fold).toBe(true);
    expect(shouldFold("某条事实 #no-sync").fold).toBe(true);
  });

  it("健康与钱各命中一类", () => {
    expect(shouldFold("这次住院三天").reason).toBe("health");
    expect(shouldFold("工资到账后先还信用卡").reason).toBe("money");
  });

  it("写作/工程类事实不折（否则手机上全是糊的，折叠就废了）", () => {
    for (const t of [
      "萧盛评歌只认听感：主歌低诉，副歌要有撕裂呐喊的穿透力",
      "云端不是本地镜像，云端也要是活的自主副本",
      "原稿红线：云物理上不可改正稿",
    ]) {
      expect(shouldFold(t).fold, t).toBe(false);
    }
  });

  it("空文本不折、不报错", () => {
    for (const t of ["", "   ", undefined, null, 123]) {
      expect(shouldFold(t as unknown).fold).toBe(false);
    }
  });

  it("每个命中类别都有面向用户的说明文案（不给工程术语）", () => {
    for (const k of ["family", "health", "money", "id", "tag"] as const) {
      expect(FOLD_LABEL[k].length).toBeGreaterThan(1);
    }
  });
});
