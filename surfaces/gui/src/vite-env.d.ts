/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 应用版本号，由 vite.config.ts 从 tauri.conf.json 注入 */
  readonly VITE_APP_VERSION: string;
}
