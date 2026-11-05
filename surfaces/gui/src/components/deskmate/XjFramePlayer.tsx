/**
 * XjFramePlayer — 小桔序列帧播放器。
 *
 * 借鉴 dsh-dafeiyu（大肥鱼）的做法：
 *   - 每个 clip 是一组预渲染好的透明 WebP 帧（AI 视频 → 绿幕抠像 → 拆帧）
 *   - 帧间 42ms 硬切（≈24fps），clip 内不做任何 CSS transform
 *   - clip 之间 100ms crossfade
 *   - 预加载全部帧，运行时只切 <img>.src，零解码开销
 *
 * 素材约定：public/pet-clips/<clipName>/frames/<clipName>_NNN.webp
 *
 * 🔴 契约：桌宠**一律无声**（萧哥 2026-11-05 定）。
 *    本播放器只吃帧序列，不得加 <audio>/Audio()/音效文件；生成素材时视频音轨
 *    在拆帧阶段就丢掉了，不要为了「有个声音更活泼」再补回音效。
 */
import { useEffect, useRef, useState, useCallback } from "react";

const FRAME_MS = 42;
const CROSSFADE_MS = 100;

export interface XjFramePlayerProps {
  /** 当前播放的 clip 名（对应 public/pet-clips/<clipName>/frames/） */
  clip: string;
  /** 该 clip 的总帧数 */
  frameCount: number;
  /** 组件渲染尺寸（px） */
  size?: number;
  /** 朝向翻转 */
  facing?: "left" | "right";
  /** 点击 */
  onClick?: () => void;
  onDoubleClick?: () => void;
}

export function XjFramePlayer({
  clip,
  frameCount,
  size = 200,
  facing = "right",
  onClick,
  onDoubleClick,
}: XjFramePlayerProps) {
  // 预加载的帧 URL 表：{ [clipName]: string[] }
  const cacheRef = useRef<Map<string, string[]>>(new Map());
  const [frameSrc, setFrameSrc] = useState<string>("");
  const [prevSrc, setPrevSrc] = useState<string>("");
  const idxRef = useRef(0);
  const timerRef = useRef<number>(0);

  // 加载某 clip 的全部帧
  const loadClip = useCallback(
    (name: string, count: number): Promise<string[]> => {
      const cached = cacheRef.current.get(name);
      if (cached) return Promise.resolve(cached);
      const urls: string[] = [];
      const prefix = `/pet-clips/${name}/frames/${name}_`;
      let loaded = 0;
      return new Promise((resolve) => {
        for (let i = 0; i < count; i++) {
          const url = `${prefix}${String(i).padStart(3, "0")}.webp`;
          urls.push(url);
          const im = new Image();
          im.onload = im.onerror = () => {
            loaded++;
            if (loaded === count) {
              cacheRef.current.set(name, urls);
              resolve(urls);
            }
          };
          im.src = url;
        }
      });
    },
    [],
  );

  // 切换 clip：加载新帧 → 100ms crossfade → 开始播放
  useEffect(() => {
    let cancelled = false;
    clearInterval(timerRef.current);
    loadClip(clip, frameCount).then((urls) => {
      if (cancelled) return;
      idxRef.current = 0;
      setPrevSrc(frameSrc);
      setFrameSrc(urls[0]);
      const fadeT = window.setTimeout(() => setPrevSrc(""), CROSSFADE_MS);
      timerRef.current = window.setInterval(() => {
        idxRef.current = (idxRef.current + 1) % urls.length;
        setFrameSrc(urls[idxRef.current]);
      }, FRAME_MS);
      return () => window.clearTimeout(fadeT);
    });
    return () => {
      cancelled = true;
      clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip, frameCount]);

  return (
    <div
      className="relative cursor-pointer select-none"
      style={{
        width: size,
        height: size,
        transform: facing === "left" ? "scaleX(-1)" : "none",
      }}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      role="button"
      tabIndex={0}
      aria-label="小桔桌宠"
    >
      <style>{`
        @keyframes xj-clip-in {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @keyframes xj-clip-out {
          from { opacity: 1; }
          to { opacity: 0; }
        }
        .xj-frame {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: contain;
          pointer-events: none;
          filter: drop-shadow(0 6px 12px rgba(0,0,0,0.12));
        }
        .xj-clip-in { animation: xj-clip-in ${CROSSFADE_MS}ms ease-out both; }
        .xj-clip-out { animation: xj-clip-out ${CROSSFADE_MS}ms ease-in both; }
      `}</style>
      {prevSrc && (
        <img src={prevSrc} alt="" aria-hidden="true" className="xj-frame xj-clip-out" draggable={false} />
      )}
      {frameSrc && (
        <img src={frameSrc} alt="小桔" className="xj-frame xj-clip-in" draggable={false} />
      )}
    </div>
  );
}
