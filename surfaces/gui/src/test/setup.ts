// Test setup: the official suite is written English-first (it asserts English UI
// literals), but the desktop app's default UI language is Chinese. Without an i18n
// instance the components' `useTranslation()` calls return the raw key, so every
// assertion on a rendered string fails. Load the app's i18n module and force the
// `en` locale so `t(key)` resolves to real English — this also exercises the en
// language bundle we ship.
import i18n from "../i18n";

i18n.changeLanguage("en");
