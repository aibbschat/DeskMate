import type { CSSProperties } from "react";
import mascotImg from "../assets/mascot-xiaoju.png";

interface XiaojuAvatarProps {
  size?: number;
  className?: string;
  style?: CSSProperties;
}

/**
 * 小桔头像 —— 圆形裁剪的透明 PNG，用于聊天气泡的发言者标识。
 * 接口与原 CicadaAvatar 兼容（size / className / style）。
 */
export default function XiaojuAvatar({ size = 18, className, style }: XiaojuAvatarProps) {
  return (
    <img
      src={mascotImg}
      alt="小桔"
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        objectFit: "cover",
        objectPosition: "center top",
        display: "block",
        ...style,
      }}
      draggable={false}
    />
  );
}
