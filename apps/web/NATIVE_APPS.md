# Veya 原生 App 程序（接 Cindy 缺口 ① 的第一片）

状态：Phase 1 已搭好壳，未上架。不要对外称“App 已上线”。

## 为什么是 Capacitor 壳，不是重写

veya 的前端是 SvelteKit（Web／SSR），重写一套 Expo／原生等于另起一个
产品。Capacitor 的做法是一个 codebase：壳里跑现有站点，之后再逐步加
原生能力（推送、分享、离线）。Cindy 的 Expo 路线适合它的 React Native
栈，不适合 veya。

## Phase 1（已做）：远程 URL 壳

- `capacitor.config.js`：appId `com.aiinote.veya`，`server.url` 指向
  https://veya.aiinote.com，壳永远等于线上站点。
- 该文件在 `pnpm build` 之外，不影响现有 Web 部署。
- PWA 已是底座（manifest＋service worker＋图标，app.html 已接 manifest
  链接），手机浏览器“添加到主屏幕”今天就能用，壳只是再往应用商店走一步。

## Phase 2（待排）：原生能力

- 推送：接 Capacitor PushNotifications，对接现有 Web Push（`server/bot_team/push.py`）。
- 离线壳：迁 adapter-static，把首屏静态化；现在 SSR 页离线打不开。
- 分享／文件：接 Share／Filesystem，只做主脑真正用到的两三处。

## Phase 3（待排）：上架

- 需要：Android Studio（Android）／Xcode＋Apple Developer（iOS）、签名 key、
  商店资料与隐私说明。构建与签名在 CI 或 Soffy 机器上做，不在本仓。
- 诚实提醒：纯 webview 壳在苹果审核里可能被要求补原生功能，Phase 2 的
  推送＋离线就是为这一步准备的，不是装饰。

## 与“手机远程桌面”的关系

手机远程面已由两处承接，不用等 App：`/remote`（移动优先的跨设备控制）
＋`/muse` 窗口页（桌面画面嵌进手机浏览器）。App 壳只是把这两处装进
商店入口，不改变它们。
