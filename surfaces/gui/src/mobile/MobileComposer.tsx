/**
 * 手机端输入区（重写，不复用桌面 Composer）。
 *
 * 桌面 Composer 有 ~28 个 props，其中模型/模式/项目绑定/表情面板/usage 胶囊/原生文件选择
 * 都是桌面 chrome（设计稿 §五判定：必须重写）。手机端只保留三件事：
 *  1. 自增高输入框（上限 6 行）；字号 16px —— iOS 小于 16px 聚焦会强制放大整页且不回弹；
 *  2. 相册选图 → `attachments:[{kind:"image",data_url}]`（走同一条 WS 帧，不新增契约）；
 *  3. 发送/停止。断线时不禁用输入：`Session` 会把消息进 outbox、重连后补发（api.ts 2026-10-05），
 *     所以「灰掉输入框」是错的，正确姿势是照发 + 顶部告诉她「连上了会自动补发」。
 */
import { useEffect, useRef, useState } from "react";
import type { Attachment } from "../types";

const MAX_ROWS = 6;
const LINE = 22; // px，与 mobile.css 里的 line-height 对齐

interface Props {
  live: boolean;
  running: boolean;
  disabled?: boolean;
  placeholder?: string;
  onSend: (text: string, attachments?: Attachment[]) => void;
  onInterrupt: () => void;
}

export function MobileComposer({ live, running, disabled, placeholder = "想跟她说点什么…", onSend, onInterrupt }: Props) {
  const [text, setText] = useState("");
  const [shots, setShots] = useState<Attachment[]>([]);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 自增高：先归零再按 scrollHeight，封顶 6 行后内部滚动
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, LINE * MAX_ROWS) + "px";
  }, [text]);

  const canSend = (text.trim().length > 0 || shots.length > 0) && !disabled;

  const submit = () => {
    if (!canSend) return;
    onSend(text, shots.length ? shots : undefined);
    setText("");
    setShots([]);
  };

  const pickImage = (file?: File | null) => {
    if (!file || !file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      if (url.startsWith("data:image/")) setShots((s) => [...s, { kind: "image", name: file.name || "image", mime: file.type, data_url: url }]);
    };
    reader.readAsDataURL(file);
  };

  return (
    <div className="mc-wrap">
      {shots.length > 0 && (
        <div className="mc-shots">
          {shots.map((a, i) => (
            <img key={i} src={a.data_url} alt={a.name} className="mc-shot" />
          ))}
          <button
            type="button"
            className="mc-shot-x"
            onClick={() => setShots((s) => s.slice(0, -1))}
            aria-label="移除这张图"
          >
            ✕
          </button>
        </div>
      )}
      <div className="mc-row">
        <button type="button" className="mc-ico" onClick={() => fileRef.current?.click()} aria-label="加图片">
          <svg width="19" height="17" viewBox="0 0 18 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M2 4.5h3l1.4-2h3.2L11 4.5h5v9H2z" />
            <circle cx="9" cy="9" r="2.6" />
          </svg>
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          className="mc-file"
          onChange={(e) => {
            pickImage(e.target.files?.[0]);
            e.target.value = ""; // 允许连续选同一张
          }}
        />
        <textarea
          ref={taRef}
          className="mc-field"
          rows={1}
          value={text}
          placeholder={placeholder}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // 手机端不做 Enter 发送（软键盘的回车就是换行）；桌面才用 Enter/⌘Enter。
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {running ? (
          <button type="button" className="mc-send stop" onClick={onInterrupt} aria-label="让她停下">
            ■
          </button>
        ) : (
          <button type="button" className={"mc-send" + (canSend ? "" : " dis")} onClick={submit} aria-label="发送">
            <svg width="17" height="17" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 2.4 13.6 8 8 13.6v-3.4H2.4V7.8H8z" />
            </svg>
          </button>
        )}
      </div>
      {!live && <div className="mc-queue">没网络也能打字 · 连上自动补发</div>}
    </div>
  );
}
