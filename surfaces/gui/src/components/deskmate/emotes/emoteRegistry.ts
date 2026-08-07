/**
 * emoteRegistry — 表情注册表查询 API（前端侧）。
 *
 * 这是「表情系统」对外的统一查询入口：系统其余部分（气泡渲染、输入框建议、
 * 未来主动提醒/勋章 toast）需要「按场景找表情」时，都走这里，而不是直接读 manifest。
 * 语义层来自默认 Pack（moling-default），多 Pack 只换画风、不改语义。
 *
 * 对标微信/QQ 的思路：表情是「有标准、可检索」的资源，而不是散落的硬编码。
 */

import { emoteList, type EmoteManifestEntry } from "./emoteManifest";

/** id → 表情条目 索引（语义层，来自默认 Pack）。 */
const BY_ID: Record<string, EmoteManifestEntry> = {};
for (const e of emoteList()) BY_ID[e.id] = e;

/** 取单个表情条目（找不到返回 undefined）。 */
export function emoteById(id: string): EmoteManifestEntry | undefined {
  return BY_ID[id];
}

/** 按情绪筛选（idle/happy/thinking/concerned/focused…）。 */
export function emotesByEmotion(emotion: string): EmoteManifestEntry[] {
  return emoteList().filter((e) => e.emotion === emotion);
}

/** 按分类筛选（greeting/celebration/writing/reaction…）。 */
export function emotesByCategory(category: string): EmoteManifestEntry[] {
  return emoteList().filter((e) => e.category === category);
}

/**
 * 按一段文本推荐最贴切的表情 id（LLM 之外的确定性回退 / 主动场景用）。
 * 优先匹配触发词（权重 2），其次匹配中文名出现在文本里（权重 1）。
 * 无命中返回 null。
 */
export function suggestEmote(text: string): string | null {
  if (!text) return null;
  let best: { id: string; score: number } | null = null;
  for (const e of emoteList()) {
    let score = 0;
    for (const t of e.triggers) {
      if (t && text.includes(t)) score += 2;
    }
    if (e.name_zh && text.includes(e.name_zh)) score += 1;
    if (score > 0 && (!best || score > best.score)) {
      best = { id: e.id, score };
    }
  }
  return best ? best.id : null;
}
