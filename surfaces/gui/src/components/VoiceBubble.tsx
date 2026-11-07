import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Item } from "../types";

type VoiceItem = Extract<Item, { kind: "voice" }>;

/**
 * 微信式语音条：默认**完全静默**，点一下才播，再点停止。
 *
 * 与「直接朗读」（说「念给我听」时从扬声器念）是两个不同出口——那条不经过这里。
 * 音频是 data: URL，随 WS 事件一次性到达，所以点击即播、无网络等待。
 * 组件卸载（切会话/刷新）时强制停掉，避免声音留在后台响。
 */
export function VoiceBubble({ item }: { item: VoiceItem }) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  // 先用服务端按码率估算的秒数定宽度，拿到真实 metadata 后再校正
  const [duration, setDuration] = useState(item.seconds || 0);

  useEffect(() => {
    return () => {
      const audio = audioRef.current;
      audioRef.current = null;
      if (!audio) return;
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
    };
  }, []);

  // 宽度用服务端估算的秒数（气泡一旦出现宽度就该定住，像微信那样不跳动）；
  // 显示的秒数优先用解码后的真实时长校正。
  const baseSeconds = Math.max(1, Math.round(item.seconds || 1));
  const shownSeconds = Math.max(1, Math.round(duration || item.seconds || 1));
  const width = Math.min(232, 68 + baseSeconds * 11);
  const label = playing
    ? t("chat.voiceStop")
    : t("chat.voicePlay", { seconds: shownSeconds });

  function stop() {
    const audio = audioRef.current;
    audioRef.current = null;
    if (!audio) return;
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    setPlaying(false);
  }

  function toggle() {
    if (playing) {
      stop();
      return;
    }
    if (typeof Audio === "undefined") return;
    const audio = new Audio(item.audio);
    audio.onloadedmetadata = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        setDuration(audio.duration);
      }
    };
    audio.onended = () => {
      audioRef.current = null;
      setPlaying(false);
    };
    audio.onerror = () => {
      audioRef.current = null;
      setPlaying(false);
    };
    audioRef.current = audio;
    setPlaying(true);
    // 播放被拒（无用户手势/解码失败）时回退到静默，绝不留下一个假装在播的按钮
    void audio.play().catch(() => {
      audioRef.current = null;
      setPlaying(false);
    });
  }

  return (
    <button
      type="button"
      className="voice-bubble"
      data-playing={playing ? "true" : "false"}
      style={{ width }}
      title={item.text}
      aria-label={label}
      onClick={toggle}
    >
      <span className="voice-waves" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span className="voice-seconds">{shownSeconds}″</span>
      {item.truncated && (
        <span className="voice-truncated">{t("chat.voiceTruncated")}</span>
      )}
    </button>
  );
}
