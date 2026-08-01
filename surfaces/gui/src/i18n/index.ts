import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "../locales/zh.json";
import en from "../locales/en.json";

export const SUPPORTED_LANGUAGES = [
  { code: "zh", label: "中文" },
  { code: "en", label: "English" },
] as const;

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]["code"];

const STORAGE_KEY = "deskmate.locale";

function initialLanguage(): LanguageCode {
  if (typeof localStorage !== "undefined") {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "zh" || saved === "en") return saved;
  }
  // Default to Chinese.
  return "zh";
}

export function setLanguage(code: LanguageCode): void {
  if (typeof localStorage !== "undefined") {
    localStorage.setItem(STORAGE_KEY, code);
  }
  i18n.changeLanguage(code);
}

i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: initialLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
  returnNull: false,
});

export default i18n;
