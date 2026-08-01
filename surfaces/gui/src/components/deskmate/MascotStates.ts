/**
 * MascotStates — 桌宠状态类型与「从 agent 运行状态推断桌宠状态」的映射。
 *
 * 早期版本的 SVG 视觉参数表（bodyColor / eyeStyle / 动画参数等）已随 BobaPet
 * 改用 petdex 雪碧帧动画而废弃，这里只保留类型与推断函数。
 */

export type MascotState =
  | "idle"        // 站立，轻微浮动
  | "walking"     // 左右走动
  | "thinking"    // 思考中（头上冒泡泡）
  | "working"     // 工作中（专注小碎步）
  | "reading"     // 阅读中（眼睛左右移动）
  | "searching"   // 搜索中（头上冒放大镜）
  | "happy"       // 开心（弹跳）
  | "concerned";  // 关注（后缩）

/**
 * 根据 agent 的运行状态推断桌宠状态。
 * 与 EmotionState 的映射保持同步：emotionToMascotState / mascotToEmotionName。
 */
export function mascotStateFromAgentState(state: string): MascotState {
  // 英文状态
  switch (state) {
    case "thinking":
    case "processing":
    case "reasoning":
      return "thinking";
    case "speaking":
    case "writing":
    case "generating":
      return "working";
    case "reading":
      return "reading";
    case "searching":
    case "browsing":
      return "searching";
    case "success":
    case "completed":
      return "happy";
    case "warning":
    case "error":
      return "concerned";
    case "walking":
      return "walking";
    case "idle":
      return "idle";
  }

  // 中文状态（App.tsx 中 dmPush 传递的中文字符串）
  const lower = state.toLowerCase();
  if (lower.includes("思考") || lower.includes("推理") || lower.includes("分析")) return "thinking";
  if (lower.includes("写") || lower.includes("回复") || lower.includes("生成") || lower.includes("调用")) return "working";
  if (lower.includes("读") || lower.includes("理解")) return "reading";
  if (lower.includes("搜") || lower.includes("查") || lower.includes("浏览")) return "searching";
  if (lower.includes("完成") || lower.includes("成功") || lower.includes("搞定")) return "happy";
  if (lower.includes("注意") || lower.includes("警告") || lower.includes("错误")) return "concerned";
  if (lower.includes("待机") || lower.includes("等待")) return "idle";

  return "idle";
}
