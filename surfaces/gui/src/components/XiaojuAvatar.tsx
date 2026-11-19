import type { CSSProperties } from "react";
// PR21(#53)：811KB 原图以 44px 渲染（线性过采样 ~23×），改用 88px 缩略（9KB）。
import mascotImg from "../assets/mascot-xiaoju-88.png";

interface XiaojuAvatarProps {
  size?: number;
  className?: string;
  style?: CSSProperties;
}

/**
 * 小桔头像 —— 固定用 src/assets/mascot-xiaoju.png（出厂立绘）。
 * 换肤只改整站配色/装饰，不改小桔自己的形象 —— 桌宠/头像永远是"同一只桔皮帽小女孩"。
 *
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
