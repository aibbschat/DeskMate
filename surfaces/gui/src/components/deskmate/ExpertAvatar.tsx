import type { Expert } from "./experts";

interface Props {
  expert: Expert;
  size?: number;
  className?: string;
}

const GRADIENTS: Record<string, { bg: string; color: string }> = {
  editor: { bg: "linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)", color: "#fff" },
  outline: { bg: "linear-gradient(135deg, #f59e0b 0%, #f97316 100%)", color: "#fff" },
  character: { bg: "linear-gradient(135deg, #f43f5e 0%, #ec4899 100%)", color: "#fff" },
  polish: { bg: "linear-gradient(135deg, #10b981 0%, #06b6d4 100%)", color: "#fff" },
  lore: { bg: "linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%)", color: "#fff" },
};

function avatarText(name: string): string {
  // 中文专家取前 1 字；英文取首字母
  const first = name.trim().charAt(0);
  return /[\u4e00-\u9fa5]/.test(first) ? first : first.toUpperCase();
}

export function ExpertAvatar({ expert, size = 40, className = "" }: Props) {
  const s = { width: size, height: size, fontSize: Math.round(size * 0.42) };

  if (expert.avatar) {
    return (
      <img
        src={expert.avatar}
        alt={expert.displayName}
        className={"expert-avatar " + className}
        style={s}
        loading="lazy"
      />
    );
  }

  const theme = GRADIENTS[expert.id] ?? { bg: "linear-gradient(135deg, #64748b 0%, #94a3b8 100%)", color: "#fff" };
  return (
    <span
      className={"expert-avatar " + className}
      style={{
        ...s,
        background: theme.bg,
        color: theme.color,
      }}
      aria-label={expert.displayName}
    >
      {avatarText(expert.displayName)}
    </span>
  );
}
