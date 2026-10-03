// ============================================
// WorkBuddy 皮肤一键注入脚本 - 「春日草地」
// 使用方法：打开 WorkBuddy 的 DevTools (Cmd+Option+I)，
// 切换到 Console 标签，粘贴本代码并回车执行
// ============================================

(function() {
  // 皮肤 CSS 内容
  const skinCSS = `
    /* 春日草地皮肤 */
    html, body {
      background: url('file:///Users/xiaocheng/workspace/openworker/workbuddy-skin/spring-grass-bg.png') 
                  bottom center / cover no-repeat fixed !important;
    }

    :root, [data-skin="spring-grass"] {
      --wb-bg-primary: #ffffff;
      --wb-bg-secondary: #f5f7f2;
      --wb-bg-card: #f8f9f5;
      --wb-bg-card-strong: #eef2e6;
      --wb-sidebar-bg: #e8efe0;
      --wb-sidebar-hover: #dfe8d4;
      --wb-sidebar-active: #d4e0c5;
      --wb-text-strong: #2d3428;
      --wb-text-medium: #5a6352;
      --wb-text-weak: #8a9182;
      --wb-border-default: #d8dfcf;
      --wb-border-light: #e8ede0;
      --wb-color-primary: #7a9e6e;
      --wb-color-primary-hover: #6a8e5e;
    }

    [class*="sidebar"], [class*="Sidebar"], [class*="left-panel"], [class*="LeftPanel"] {
      background-color: var(--wb-sidebar-bg) !important;
      border-right: 1px solid var(--wb-border-light) !important;
    }

    [class*="main-content"], [class*="MainContent"], 
    [class*="chat-content"], [class*="ChatContent"],
    [class*="conversation"], [class*="Conversation"],
    [class*="setting-panel"], [class*="SettingPanel"] {
      background-color: #ffffff !important;
    }

    [class*="title-bar"], [class*="TitleBar"], [class*="header"], [class*="Header"] {
      background-color: rgba(255, 255, 255, 0.85) !important;
      backdrop-filter: blur(10px);
    }

    [class*="welcome"], [class*="Welcome"], 
    [class*="home-page"], [class*="HomePage"] {
      background-color: rgba(255, 255, 255, 0.4) !important;
    }

    [class*="chat-input"], [class*="ChatInput"] {
      background-color: #f8f9f5 !important;
      border: 1px solid var(--wb-border-light) !important;
    }

    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: transparent;
    }
    ::-webkit-scrollbar-thumb {
      background: #c8d4b8;
      border-radius: 3px;
    }
  `;

  // 移除旧的皮肤样式
  const oldStyle = document.getElementById('custom-spring-grass-skin');
  if (oldStyle) oldStyle.remove();

  // 创建并注入新样式
  const style = document.createElement('style');
  style.id = 'custom-spring-grass-skin';
  style.textContent = skinCSS;
  document.head.appendChild(style);

  console.log('%c🌿 春日草地皮肤已加载！', 'color: #7a9e6e; font-size: 14px; font-weight: bold;');
  console.log('如果效果不对，可以告诉我哪里需要调整～');
})();
