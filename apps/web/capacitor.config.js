/** Capacitor shell for Veya (Phase 1: remote-URL shell, see NATIVE_APPS.md).
 *
 * This file is inert until the Capacitor CLI runs it: it does not affect
 * `pnpm build` or the web deployment. The shell loads the live site
 * (https://veya.aiinote.com) in a native webview, so the store app always
 * matches the web app. A fully offline static shell is Phase 2 and needs
 * an adapter-static migration; it is NOT claimed here.
 *
 * Store builds need Android Studio / Xcode + signing keys (CI or owner
 * machine step, not this repo).
 */
const config = {
  appId: "com.aiinote.veya",
  appName: "Veya",
  webDir: "build/client",
  server: {
    url: "https://veya.aiinote.com",
    androidScheme: "https",
    cleartext: false,
  },
};

export default config;
