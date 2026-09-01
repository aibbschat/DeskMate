/**
 * 会话时间戳解析的**唯一入口**。
 *
 * 后端 `sessions.updated_at` 由 SQLite `CURRENT_TIMESTAMP` 写入，格式
 * "YYYY-MM-DD HH:MM:SS"，**UTC 且不带时区标记**。这个组合踩过两次坑：
 *
 *  1. Safari/WebKit 的 `Date.parse` 不认空格分隔格式 → NaN → 所有会话 ts=0，
 *     启动恢复时排序失效、挑错会话（桌搭聊天"重启后消失"）。
 *  2. 直接 `Date.parse` 在 Chromium 下会按**本地时区**解读这个 UTC 串 →
 *     相对时间凭空多出一个 UTC 偏移（东八区刚聊完显示 "8h" 而不是 "now"，
 *     侧栏看着像这个会话很久没动过）。
 *
 * 一律先补 "Z" 再解析；排序和显示都必须走这里，不要各自 Date.parse。
 */
export function parseTs(s: string | number | undefined | null): number {
  if (s == null) return 0;
  if (typeof s === "number") return s;
  const str = String(s).trim();
  if (!str) return 0;
  const iso = str.includes("T") ? str : str.replace(" ", "T");
  // 日期-only（如 "2026-06-29"，侧栏测试与部分旧记录用）本身就是 UTC 午夜语义，
  // 补 "Z" 在 WebKit 上反而可能解析失败 —— 没有时间部分就原样交给引擎。
  const hasTime = /\d{1,2}:\d{2}/.test(iso);
  const zoned =
    !hasTime || /[zZ]$/.test(iso) || /[+-]\d{2}:?\d{2}$/.test(iso)
      ? iso
      : iso + "Z";
  const parsed = Date.parse(zoned);
  if (!Number.isNaN(parsed)) return parsed;
  const fb = Date.parse(str); // 兜底：交给引擎自行解析
  return Number.isNaN(fb) ? 0 : fb;
}
