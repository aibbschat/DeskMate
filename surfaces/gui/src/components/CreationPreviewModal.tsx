// 创作流水线产出预览弹窗：把大纲/剧本/审核意见放到舒适的阅读器里看，
// 而不是挤在流程卡片下方的内联小框里。
import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "./Icon";

export function CreationPreviewModal({
  open,
  title,
  content,
  onClose,
  loading,
}: {
  open: boolean;
  title: string;
  content: string;
  onClose: () => void;
  loading?: boolean;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);

  // ESC 关闭；打开时锁定底层滚动
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const original = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = original;
    };
  }, [open, onClose]);

  // 每次打开新内容时滚动到顶部
  useEffect(() => {
    if (open && bodyRef.current) {
      bodyRef.current.scrollTop = 0;
    }
  }, [open, title, content]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      style={{ padding: "24px" }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="modal creation-preview-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="creation-preview-title"
      >
        <div className="modal-head">
          <h2 id="creation-preview-title" className="modal-title">
            <Icon name="bookOpen" size={16} />
            <span className="truncate max-w-[560px]" title={title}>
              {title || "内容预览"}
            </span>
          </h2>
          <button
            className="modal-x"
            aria-label="关闭预览"
            onClick={onClose}
            title="关闭 (Esc)"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        <div ref={bodyRef} className="creation-preview-body">
          {loading ? (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-muted">
              <span className="cre-pulse-dot inline-block w-2 h-2 rounded-full bg-accent" />
              正在加载预览…
            </div>
          ) : content ? (
            <article className="creation-preview-article">
              <ReactMarkdown>{content}</ReactMarkdown>
            </article>
          ) : (
            <div className="flex flex-col items-center justify-center h-full gap-2 text-muted">
              <Icon name="file" size={32} />
              暂无内容
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
