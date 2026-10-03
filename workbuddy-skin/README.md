# 🌿 WorkBuddy 皮肤 - 「春日草地」

自然治愈系风格的自定义皮肤，装饰元素全在边缘，中间工作区干净不挡字。

## 📁 文件说明

| 文件 | 说明 |
|------|------|
| `spring-grass-bg.png` | 背景装饰图 |
| `spring-grass.css` | 皮肤样式源码 |
| `apply-skin.js` | 一键注入脚本 |

## 🚀 快速使用（推荐）

### 方法一：DevTools 一键注入（临时）

1. 打开 WorkBuddy
2. 按 `Cmd + Option + I` 打开开发者工具
3. 切换到 **Console** 标签
4. 把 `apply-skin.js` 的内容全部粘贴进去，回车执行
5. 皮肤立即生效！

> 注意：这种方式是临时的，重启 WorkBuddy 后需要重新执行一次。

---

### 方法二：持久化（推荐长期用）

#### 步骤 1：先应用一个官方皮肤
1. 打开 WorkBuddy 设置 → 外观
2. 随便选一个个性皮肤（比如「有风」），应用它
3. 等皮肤加载完，确认能看到效果

#### 步骤 2：替换 CSS 缓存
1. 按 `Cmd + Option + I` 打开 DevTools
2. 切换到 **Console** 标签
3. 粘贴下面这段代码，回车执行：

```javascript
// 读取我们的 CSS 内容，替换到 localStorage 缓存里
fetch('file:///Users/xiaocheng/workspace/openworker/workbuddy-skin/spring-grass.css')
  .then(r => r.text())
  .then(css => {
    // 找到 CSS 缓存的 key
    const keys = Object.keys(localStorage).filter(k => k.includes('appearance') || k.includes('css'));
    console.log('找到的缓存 key:', keys);
    
    // 手动写入缓存
    const cacheData = {
      resourceKey: 'custom-spring-grass',
      css: css,
      identity: '',
      updatedAt: Date.now()
    };
    
    // 尝试常见的 key 名
    const possibleKeys = [
      'workbuddy:appearance:css-cache',
      'appearance_css_cache',
      'wb-appearance-css'
    ];
    
    possibleKeys.forEach(key => {
      localStorage.setItem(key, JSON.stringify(cacheData));
    });
    
    console.log('%c✅ 已写入缓存，重启 WorkBuddy 后生效', 'color: green; font-size: 14px;');
    console.log('如果重启后没生效，可能需要手动找一下正确的 localStorage key');
  });
```

4. 重启 WorkBuddy

---

## 🎨 设计说明

### 布局思路
- **装饰全在边缘**：左下角雏菊草地、右下角橘猫、左上角绿叶
- **中间工作区纯白**：对话内容区完全不透明，保证阅读清晰
- **对话页无装饰**：进入对话后背景被白色内容区盖住，只保留色调

### 配色方案
- 主背景：纯白色 `#ffffff`
- 侧边栏：浅豆绿 `#e8efe0`
- 强调色：草木绿 `#7a9e6e`
- 文字：深灰绿 `#2d3428`

## 🔧 自定义调整

### 换背景图
直接替换 `spring-grass-bg.png` 文件即可，注意保持尺寸比例（建议 16:9）。

### 调配色
打开 `spring-grass.css`，修改 `:root` 里的 CSS 变量：
- `--wb-sidebar-bg`：侧边栏颜色
- `--wb-color-primary`：强调色（按钮、选中态）
- `--wb-text-strong`：主要文字颜色

## ⚠️ 注意事项

1. 皮肤效果可能需要根据实际界面微调，因为类名是猜测的
2. 如果某些区域没生效，可以打开 DevTools 检查元素，告诉我具体的类名，我来调整
3. 官方应用更新后可能会改变类名，需要重新调整
