/**
 * emotes.tsx — 小桔表情包渲染资源（v2：3D 黏土贴纸 webp）。
 *
 * v2 起废弃多 Pack SVG 系统，唯一 pack「xiaoju-clay」：16 张 3D 软胶黏土潮玩风
 * 小桔大头贴纸（透明底 webp，2048×2048），直接 <img> 渲染。
 *
 * PR21(#53)：显示尺寸只有 48–52px，2048² 原图单张 250–326KB、解码 4M 像素；
 * 改引 256px 缩略（thumbs/，单张 11–13KB），视觉无差别。原图保留备高清导出。
 * 语义见 emotes.manifest.json；本文件只做 id → 图片资源 的映射。
 */

import wave from "../../../assets/emotes/thumbs/wave.webp";
import cheer from "../../../assets/emotes/thumbs/cheer.webp";
import shy from "../../../assets/emotes/thumbs/shy.webp";
import v from "../../../assets/emotes/thumbs/v.webp";
import sleepy from "../../../assets/emotes/thumbs/sleepy.webp";
import tea from "../../../assets/emotes/thumbs/tea.webp";
import hug from "../../../assets/emotes/thumbs/hug.webp";
import facepalm from "../../../assets/emotes/thumbs/facepalm.webp";
import peek from "../../../assets/emotes/thumbs/peek.webp";
import clap from "../../../assets/emotes/thumbs/clap.webp";
import lol from "../../../assets/emotes/thumbs/lol.webp";
import think from "../../../assets/emotes/thumbs/think.webp";
import cry from "../../../assets/emotes/thumbs/cry.webp";
import shock from "../../../assets/emotes/thumbs/shock.webp";
import heart from "../../../assets/emotes/thumbs/heart.webp";
import rage from "../../../assets/emotes/thumbs/rage.webp";

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
