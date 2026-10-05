/**
 * 手机端展示层敏感折叠（D3 已拍：先展示层折叠、可点开；第二批再评估给读口加标签）。
 *
 * 为什么手机端要自己做：`deskmate/memory/sensitivity.py` 的 classify()/redact() 只作用在
 * **写入与日志**，`deskmate/agency/visibility_gate.py` 的家人词表只挡**发布到 AI 圈**那条路；
 * `GET /v1/deskmate/learned-history` 与 `/moments` 是**原样返回**的。手机是在地铁上用的设备，
 * 所以这里补一道展示层启发式。
 *
 * 词表口径（本仓库既有教训）：**中文只收双字及以上的词**——单字（家/书/信/病）必然误伤，
 * 一旦误伤用户就会不信这个折叠，比不折更糟。宁可漏，不可乱折。
 *
 * 这是启发式，会在 UI 上明写「可能误判」，不冒充后端判定。
 */

export interface FoldVerdict {
  fold: boolean;
  /** 命中类别（给折叠行的说明用） */
  reason?: "family" | "health" | "money" | "id" | "tag";
}

/** 家人相关（萧盛的唯一隐私硬线：家人）。双字以上，避免「家」字误伤。 */
const FAMILY = [
  "妈妈", "妈的", "父亲", "爸爸", "哥哥", "姐姐", "弟弟", "妹妹", "外婆", "外公",
  "奶奶", "爷爷", "叔叔", "阿姨", "姑姑", "舅舅", "表妹", "表弟", "堂兄", "堂姐",
  "儿子", "女儿", "老婆", "老公", "妻子", "丈夫", "家里人", "家人", "亲属",
];

/** 身体健康（体检/复查/指标这类，落在私域设备上显眼） */
const HEALTH = ["体检", "复查", "指标", "住院", "手术", "诊断", "病历", "吃药", "用药", "血压", "血糖", "癌症", "肿瘤"];

/** 财务与证件 */
const MONEY = ["工资", "余额", "银行卡", "信用卡", "转账", "还款", "贷款", "理财", "股票账户", "密码"];
const ID = ["身份证", "护照号", "社保", "住址", "门牌", "家庭地址"];

/** 后端在写入时可能留下的显式标签（sensitivity.py 的 #private/#no-sync/#sensitive） */
const TAG_RE = /#(private|no-sync|sensitive)\b/i;

/** 手机号 / 18 位证件号形态 */
const PII_RE = /(?:1[3-9]\d{9})|(?:\d{17}[\dxX])/;

function hit(text: string, words: readonly string[]): string | null {
  for (const w of words) if (text.includes(w)) return w;
  return null;
}

/**
 * 判定一条文本是否该在手机上默认折叠。
 * 输入是后端原样文本；空串/undefined 一律不折（不制造无谓的空白）。
 */
export function shouldFold(text: unknown): FoldVerdict {
  if (typeof text !== "string" || !text.trim()) return { fold: false };
  if (TAG_RE.test(text)) return { fold: true, reason: "tag" };
  if (PII_RE.test(text)) return { fold: true, reason: "id" };
  if (hit(text, FAMILY)) return { fold: true, reason: "family" };
  if (hit(text, HEALTH)) return { fold: true, reason: "health" };
  if (hit(text, MONEY)) return { fold: true, reason: "money" };
  if (hit(text, ID)) return { fold: true, reason: "id" };
  return { fold: false };
}

/** 折叠行的说明文案（用户角度，不写工程术语） */
export const FOLD_LABEL: Record<NonNullable<FoldVerdict["reason"]>, string> = {
  family: "涉及家人",
  health: "涉及健康",
  money: "涉及钱",
  id: "涉及证件或号码",
  tag: "你标过「别上云」",
};
