/**
 * emoteManifest — 前端读取「小桔表情接入标准」(emotes.manifest.json) 的入口。
 *
 * manifest 是前后端共用的单一真相源：定义有哪些表情、各自中文名/分类/含义/触发场景。
 * 本文件把它拍平成一个好用的列表，供表情栏(EmotePicker)、渲染(Emote)、输入框(Composer)消费。
 * 渲染实现(SVG)不在这里——见 emotes.tsx 的 SVG_REGISTRY。
 */
import manifest from "./emotes.manifest.json";

export interface EmoteManifestEntry {
  id: string;
  name_zh: string;
  name_en: string;
  category: string;
  meaning: string;
  triggers: string[];
  emotion?: string;
}

export interface EmotePack {
  id: string;
  name: string;
  style: string;
  license: string;
  /** 本 pack 自带语义表情；若用 inherit 继承另一 pack，则可省略 */
  emotes?: EmoteManifestEntry[];
  /** 继承另一 pack 的语义表情（同 id 换画风），用于「换皮」专辑 */
  inherit?: string;
}

export interface EmoteManifest {
  version: string;
  defaultPack: string;
  description?: string;
  packs: Record<string, EmotePack>;
}

export const EMOTE_MANIFEST = manifest as EmoteManifest;

/** 拍平所有 pack 的语义表情（inherit 的 pack 无自带 emotes，按空处理）。 */
export function emoteList(): EmoteManifestEntry[] {
  return Object.values(EMOTE_MANIFEST.packs).flatMap((p) => p.emotes ?? []);
}

/** 取表情中文名（找不到回落到 id）。 */
export function emoteLabel(id: string): string {
  for (const p of Object.values(EMOTE_MANIFEST.packs)) {
    const found = p.emotes?.find((e) => e.id === id);
    if (found) return found.name_zh;
  }
  return id;
}
