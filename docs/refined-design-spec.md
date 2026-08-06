# StatusPanel 精致设计规范

## 一、精致设计的核心原则

### 1.1 拒绝粗糙
**问题诊断**：
- 边框太粗（1px solid）、颜色太深（#00000020）
- 进度条太扁平、没有质感
- 字体大小跳跃（10px → 12px → 20px）、层次生硬
- 阴影太重或不存在、缺少层次感
- 动画太突然、没有缓动曲线

**精致目标**：
- 边框像"空气"一样轻盈，存在但不突兀
- 元素像"漂浮"在面板上，有微妙的深度
- 颜色像"水墨"一样柔和，过渡自然
- 动画像"呼吸"一样流畅，不抢注意力

---

## 二、边框规范

### 2.1 厚度
```
主边框：0.5px（或 1px 透明度 50%）
分割线：0.5px
强调边框：1px（仅用于特殊元素）
```

### 2.2 颜色
```
普通边框：rgba(0, 0, 0, 0.06)  // 几乎看不见，但能分隔
hover 边框：rgba(0, 0, 0, 0.12)  // 微微加深
强调边框：rgba(99, 102, 241, 0.2)  // 品牌色，非常淡
```

### 2.3 圆角
```
小元素（徽章、标签）：6px
中元素（卡片、按钮）：10px
大元素（面板本身）：14px
超大元素（弹窗）：18px

注意：圆角要"润"，不能太尖也不能太圆
```

### 2.4 示例
```css
/* ❌ 粗糙 */
border: 1px solid #e5e7eb;

/* ✅ 精致 */
border: 0.5px solid rgba(0, 0, 0, 0.06);
/* 或者 */
border: 1px solid rgba(0, 0, 0, 0.04);
box-shadow: 0 0 0 0.5px rgba(0, 0, 0, 0.05);  /* 更柔和 */
```

---

## 三、阴影规范

### 3.1 层级系统
```
Level 0（平面）：无阴影
Level 1（微浮）：0 1px 2px rgba(0,0,0,0.04)
Level 2（轻浮）：0 2px 8px rgba(0,0,0,0.06)
Level 3（中浮）：0 4px 16px rgba(0,0,0,0.08)
Level 4（重浮）：0 8px 32px rgba(0,0,0,0.10)
```

### 3.2 使用场景
```
卡片默认：Level 1
卡片 hover：Level 2（提升 1 级）
下拉菜单：Level 3
模态弹窗：Level 4
进度条：无阴影（扁平设计）
```

### 3.3 内阴影（增加质感）
```
输入框聚焦：inset 0 1px 2px rgba(0,0,0,0.06)
进度条轨道：inset 0 1px 2px rgba(0,0,0,0.08)
凹陷效果：inset 0 2px 4px rgba(0,0,0,0.10)
```

### 3.4 示例
```css
/* ❌ 粗糙 */
box-shadow: 0 12px 36px rgba(0,0,0,0.14);

/* ✅ 精致 */
box-shadow:
  0 1px 2px rgba(0,0,0,0.04),   /* 第一层：贴近 */
  0 4px 12px rgba(0,0,0,0.06);  /* 第二层：扩散 */
```

---

## 四、颜色规范

### 4.1 基础色板（柔和版）
```
背景色：
  - 主背景：var(--panel) 或 #ffffff
  - 次背景：var(--paper) 或 #f9fafb
  - 悬浮背景：rgba(0, 0, 0, 0.02)

文字色：
  - 主文字：var(--ink) 或 #111827
  - 次文字：var(--muted) 或 #6b7280
  - 辅助文字：var(--faint) 或 #9ca3af
  - 禁用文字：rgba(0, 0, 0, 0.25)

边框色：
  - 主边框：rgba(0, 0, 0, 0.06)
  - 次边框：rgba(0, 0, 0, 0.04)
  - 强调边框：rgba(99, 102, 241, 0.15)
```

### 4.2 状态色（低饱和度）
```
心情/活力：
  - 高（>70%）：#059669 (绿) → 渐变 #10b981 → #34d399
  - 中（40-70%）：#d97706 (黄) → 渐变 #f59e0b → #fbbf24
  - 低（<40%）：#dc2626 (红) → 渐变 #ef4444 → #f87171

注意：避免纯色，用渐变增加质感
```

### 4.3 进度条渐变
```css
/* 心情/活力 - 高 */
background: linear-gradient(90deg, #059669, #34d399);

/* 心情/活力 - 中 */
background: linear-gradient(90deg, #d97706, #fbbf24);

/* 心情/活力 - 低 */
background: linear-gradient(90deg, #dc2626, #f87171);

/* 关系指标 */
background: linear-gradient(90deg, #4f46e5, #818cf8);

/* 成长进度 */
background: linear-gradient(90deg, #f59e0b, #fcd34d);
```

### 4.4 稀有度色彩（精致版）
```
普通（common）：
  - 背景：#f9fafb
  - 边框：rgba(0, 0, 0, 0.08)
  - 文字：#6b7280

稀有（rare）：
  - 背景：#eff6ff
  - 边框：rgba(59, 130, 246, 0.2)
  - 文字：#2563eb

史诗（epic）：
  - 背景：#f5f3ff
  - 边框：rgba(139, 92, 246, 0.2)
  - 文字：#7c3aed

传说（legendary）：
  - 背景：#fffbeb
  - 边框：rgba(245, 158, 11, 0.25)
  - 文字：#d97706
```

---

## 五、字体规范

### 5.1 字体栈
```css
font-family:
  -apple-system,
  BlinkMacSystemFont,
  "SF Pro Text",        /* macOS 系统字体 */
  "Helvetica Neue",
  "Segoe UI",
  Roboto,
  "Noto Sans SC",       /* 中文 */
  sans-serif;
```

### 5.2 字号层次（6 级）
```
Level 1（大标题）：20px, weight: 700, tracking: -0.02em
  用途：关键数值（等级、心情数值）

Level 2（小标题）：13px, weight: 600, tracking: -0.01em
  用途：区块标题、重要标签

Level 3（正文）：12px, weight: 400, tracking: 0
  用途：主要文字内容

Level 4（辅助）：11px, weight: 400, tracking: 0.01em
  用途：次要信息、描述文字

Level 5（标签）：10px, weight: 500, tracking: 0.02em, uppercase
  用途：分类标签（"关系轨"、"状态轨"）

Level 6（微文字）：9px, weight: 400, tracking: 0.02em
  用途：时间戳、极次要信息
```

### 5.3 行高
```
单行文字：1.4
多行文字：1.5
标题文字：1.3
紧凑布局：1.2
```

### 5.4 示例
```css
/* ❌ 粗糙 */
font-size: 12.5px;

/* ✅ 精致 */
font-size: 12px;
line-height: 1.4;
letter-spacing: 0;
-webkit-font-smoothing: antialiased;  /* macOS 字体平滑 */
```

---

## 六、进度条规范

### 6.1 轨道
```
高度：4px（不要太厚）
圆角：2px（与高度匹配）
背景：rgba(0, 0, 0, 0.06) 或 var(--line)
内阴影：inset 0 1px 1px rgba(0,0,0,0.06)
```

### 6.2 填充条
```
圆角：与轨道一致（2px）
渐变：从左到右，颜色加深 10%
光泽：顶部 1px 高光（rgba(255,255,255,0.3)）
动画：宽度变化时 transition 0.5s ease-out
```

### 6.3 高级质感
```css
/* 轨道 */
.progress-track {
  height: 4px;
  border-radius: 2px;
  background: rgba(0, 0, 0, 0.06);
  box-shadow: inset 0 1px 1px rgba(0,0,0,0.06);
}

/* 填充 */
.progress-fill {
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, #059669, #34d399);
  box-shadow: 0 0 2px rgba(16, 185, 129, 0.3);  /* 微发光 */
  transition: width 0.5s ease-out;
  position: relative;
}

/* 高光 */
.progress-fill::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 50%;
  background: linear-gradient(to bottom, rgba(255,255,255,0.3), transparent);
  border-radius: 2px 2px 0 0;
}
```

---

## 七、动画规范

### 7.1 时长
```
快速响应（hover、focus）：100ms
标准过渡（展开、切换）：200ms
复杂动画（弹窗、翻转）：300ms
缓慢动画（进度条、淡入）：500ms
```

### 7.2 缓动曲线
```
标准缓动：cubic-bezier(0.4, 0, 0.2, 1)  // ease-out
弹性缓动：cubic-bezier(0.34, 1.56, 0.64, 1)  // 弹跳
平滑缓动：cubic-bezier(0.25, 0.1, 0.25, 1)  // linear
进入动画：cubic-bezier(0, 0, 0.2, 1)  // ease-out
退出动画：cubic-bezier(0.4, 0, 1, 1)  // ease-in
```

### 7.3 常用动画
```css
/* 悬浮提升 */
.hover-lift {
  transition: transform 200ms cubic-bezier(0.4, 0, 0.2, 1),
              box-shadow 200ms cubic-bezier(0.4, 0, 0.2, 1);
}
.hover-lift:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(0,0,0,0.08);
}

/* 折叠展开 */
.collapse {
  overflow: hidden;
  transition: max-height 200ms cubic-bezier(0.4, 0, 0.2, 1),
              opacity 200ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* 按钮点击 */
.button-active:active {
  transform: scale(0.98);
  transition: transform 100ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* 渐入 */
.fade-in {
  animation: fadeIn 200ms cubic-bezier(0, 0, 0.2, 1);
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
```

### 7.4 禁止的动画
```
❌ 闪烁动画
❌ 弹跳过度（超过 1.1 缩放）
❌ 快速旋转（头晕）
❌ 无缓动的线性动画（生硬）
```

---

## 八、间距规范

### 8.1 间距系统（4px 基数）
```
4px（xs）：图标与文字间距
8px（sm）：紧凑元素间距
12px（md）：标准元素间距
16px（lg）：区块间距
24px（xl）：大区块间距
32px（2xl）：超大间距（很少用）
```

### 8.2 内边距
```
小元素（标签、徽章）：4px 8px
中元素（卡片、按钮）：8px 12px
大元素（面板、弹窗）：16px 20px
```

### 8.3 示例
```css
/* ❌ 粗糙 */
padding: 3.5px;  /* 奇数像素 */
gap: 7px;

/* ✅ 精致 */
padding: 4px 8px;  /* 4 的倍数 */
gap: 8px;
```

---

## 九、图标规范

### 9.1 尺寸
```
微图标：12px（行内装饰）
小图标：14px（按钮图标）
标准图标：16px（列表图标）
大图标：20px（标题图标）
超大图标：24px（特殊强调）
```

### 9.2 颜色
```
默认：var(--muted) 或 currentColor
hover：var(--ink)
active：var(--accent)
disabled：rgba(0, 0, 0, 0.25)
```

### 9.3 与文字对齐
```css
.icon-text {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.icon-text svg {
  flex-shrink: 0;  /* 不压缩 */
}
```

---

## 十、卡片设计

### 10.1 默认状态
```css
.card {
  background: var(--panel);
  border: 0.5px solid rgba(0, 0, 0, 0.06);
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  padding: 12px;
  transition: box-shadow 200ms cubic-bezier(0.4, 0, 0.2, 1);
}
```

### 10.2 Hover 状态
```css
.card:hover {
  box-shadow:
    0 1px 2px rgba(0, 0, 0, 0.04),
    0 4px 12px rgba(0, 0, 0, 0.06);
}
```

### 10.3 Active/Selected 状态
```css
.card.active {
  border-color: rgba(99, 102, 241, 0.2);
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.1);
}
```

---

## 十一、精致检查清单

在完成每个组件后，检查以下项目：

### 边框
- [ ] 厚度是否 ≤ 1px？
- [ ] 颜色是否足够柔和（透明度 ≥ 80%）？
- [ ] 圆角是否统一（4/6/8/10/14px）？

### 阴影
- [ ] 是否使用了多层阴影（而非单层）？
- [ ] 透明度是否 ≤ 10%？
- [ ] 是否在 hover 时提升了阴影层级？

### 颜色
- [ ] 是否避免了纯黑（#000）和纯白（#fff）？
- [ ] 是否使用了渐变增加质感？
- [ ] 饱和度是否适中（不要太鲜艳）？

### 字体
- [ ] 是否使用了系统字体栈？
- [ ] 是否启用了字体平滑（-webkit-font-smoothing）？
- [ ] 字间距是否合适（标题紧、正文松）？

### 动画
- [ ] 时长是否在 100-500ms 范围？
- [ ] 是否使用了贝塞尔曲线（而非 linear）？
- [ ] 是否避免了闪烁和过度弹跳？

### 间距
- [ ] 是否使用了 4px 的倍数？
- [ ] 是否避免了奇数像素？
- [ ] 间距是否有层次（小/中/大）？

---

## 十二、参考资源

### 设计系统
- [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines)
- [Material Design 3](https://m3.material.io/)
- [Ant Design](https://ant.design/)

### 精致 UI 案例
- Linear App（极简精致）
- Raycast（macOS 原生感）
- Arc Browser（创新交互）
- Notion（优雅简洁）

### CSS 技巧
- [Modern CSS Reset](https://piccalil.li/blog/a-modern-css-reset/)
- [CSS Shadows](https://css-tricks.com/snippets/css/css-box-shadow/)
- [Smooth Animations](https://web.dev/animations-guide/)

---

**记住：精致 = 细节 + 一致性 + 克制**

不要堆砌特效，而是让每个像素都恰到好处。
