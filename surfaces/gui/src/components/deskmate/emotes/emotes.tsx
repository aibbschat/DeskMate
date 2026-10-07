/**
 * emotes.tsx — 小桔表情包渲染资源（v2：3D 黏土贴纸 webp）。
 *
 * v2 起废弃多 Pack SVG 系统，唯一 pack「xiaoju-clay」：16 张 3D 软胶黏土潮玩风
 * 小桔大头贴纸（透明底 webp，2048×2048），直接 <img> 渲染。
 * 语义见 emotes.manifest.json；本文件只做 id → 图片资源 的映射。
 */

import wave from "../../../assets/emotes/wave.webp";
import cheer from "../../../assets/emotes/cheer.webp";
import shy from "../../../assets/emotes/shy.webp";
import v from "../../../assets/emotes/v.webp";
import sleepy from "../../../assets/emotes/sleepy.webp";
import tea from "../../../assets/emotes/tea.webp";
import hug from "../../../assets/emotes/hug.webp";
import facepalm from "../../../assets/emotes/facepalm.webp";
import peek from "../../../assets/emotes/peek.webp";
import clap from "../../../assets/emotes/clap.webp";
import lol from "../../../assets/emotes/lol.webp";
import think from "../../../assets/emotes/think.webp";
import cry from "../../../assets/emotes/cry.webp";
import shock from "../../../assets/emotes/shock.webp";
import heart from "../../../assets/emotes/heart.webp";
import rage from "../../../assets/emotes/rage.webp";

/** id → webp 资源 URL。 */
export const EMOTE_IMAGES: Record<string, string> = {
  wave,
  cheer,
  shy,
  v,
  sleepy,
  tea,
  hug,
  facepalm,
  peek,
  clap,
  lol,
  think,
  cry,
  shock,
  heart,
  rage,
};

/** 当前唯一 pack id（兼容旧引用）。 */
export const ACTIVE_PACK_ID = "xiaoju-clay";
